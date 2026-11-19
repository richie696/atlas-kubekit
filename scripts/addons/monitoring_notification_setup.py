# SPDX-License-Identifier: MIT
"""Guided first-time setup for one Alertmanager notification route."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lib.i18n import translate
from scripts.operations import configuration_cluster
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.environment import Environment
from scripts.operations.models import OperationError
from scripts.operations.monitoring import log_diagnostics, relay_publication
from scripts.operations.monitoring import notification_secrets
from scripts.operations.monitoring.channels import dingtalk, feishu, wecom


def tr(lang, key, *values):
    return translate(lang, key, *values)


def ask(lang, key, default=None, secret=False):
    prompt = tr(lang, key)
    if default is not None:
        prompt += f" [{default}]"
    prompt += ": "
    value = getpass.getpass(prompt) if secret else input(prompt)
    if secret:
        return value
    value = value.strip()
    return value or (default if default is not None else "")


def run(command, *, input_text=None, check=True, capture=False):
    return subprocess.run(command, input=input_text, text=True, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def kubectl_json(args):
    result = run(["kubectl", *args, "-o", "json"], capture=True)
    return json.loads(result.stdout)


def unique_name(prefix):
    return prefix + "-" + secrets.token_hex(4)


def validate_webhook(kind, url):
    validators = {"wecom": wecom.validate_endpoint, "dingtalk": dingtalk.validate_endpoint,
                  "feishu": feishu.validate_endpoint}
    try:
        validators[kind](url)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid channel endpoint") from None
    return url


def create_secret(namespace, name, values, component, receiver=None):
    manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": namespace,
                             "labels": {"atlas-kubekit.io/managed-by": "monitoring",
                                        **notification_secrets.labels(component, receiver)}},
                "data": {key: base64.b64encode(value.encode()).decode("ascii")
                         for key, value in values.items()}}
    run(["kubectl", "create", "-f", "-"],
        input_text=json.dumps(manifest, separators=(",", ":")))


def delete_secret(namespace, name):
    run(["kubectl", "delete", "secret", name, "-n", namespace, "--ignore-not-found=true"], check=False)


def verify_cluster_configuration(lang):
    """Confirm the 02-owned inventory exists and matches this live API before asking for secrets."""
    kubeconfig = os.environ.get("KUBECONFIG", "/etc/kubernetes/admin.conf")
    try:
        client = Kubernetes(kubeconfig)
        config = configuration_cluster.read(client)
        if config is None:
            raise ValueError("cluster configuration is missing")
        Environment(client).verify(config)
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(tr(lang, "phase03.monitoring.setup_requires_cluster_configuration")) from None


def monitoring_topology_mode(namespace):
    """Read the local topology marker; fail closed when its state is ambiguous."""
    result = run(["kubectl", "-n", namespace, "get", "configmap", "atlas-monitoring-topology",
                  "--ignore-not-found", "-o", "json"], check=False, capture=True)
    if result.returncode:
        raise ValueError("Unable to determine monitoring topology")
    raw = getattr(result, "stdout", "") or "{}"
    try:
        marker = json.loads(raw)
    except ValueError:
        raise ValueError("Unable to determine monitoring topology") from None
    if not marker.get("metadata"):
        return ""
    mode = marker.get("data", {}).get("mode")
    if mode not in ("center", "agent"):
        raise ValueError("Invalid monitoring topology marker")
    return mode


def alertmanager_identity(namespace, lang):
    document = kubectl_json(["get", "alertmanagers.monitoring.coreos.com", "-n", namespace])
    items = document.get("items", [])
    if len(items) != 1:
        raise ValueError(tr(lang, "phase03.monitoring.setup_alertmanager_count", len(items)))
    item = items[0]
    version = item.get("spec", {}).get("version")
    name = item.get("metadata", {}).get("name")
    if not isinstance(version, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise ValueError(tr(lang, "phase03.monitoring.setup_alertmanager_version"))
    release = item.get("metadata", {}).get("labels", {}).get("release", "kube-prometheus-stack")
    return name, version.removeprefix("v"), release


def plan_and_apply(lang, operation, args, plan_path, secret_names, state):
    executable = ROOT / "06-maintain-cluster.sh"
    kubeconfig = os.environ.get("KUBECONFIG", "/etc/kubernetes/admin.conf")
    base = ["bash", str(executable), "--lang", lang, "--kubeconfig", kubeconfig,
            "--change-plan", "--operation", operation,
            "--monitoring-namespace", args["namespace"], "--monitoring-release", args["release"],
            "--monitoring-alertmanager-name", args["alertmanager"],
            "--monitoring-alertmanager-version", args["alertmanager_version"],
            "--monitoring-relay-image", args["relay_image"],
            "--monitoring-relay-auth-secret", args["relay_secret"],
            "--monitoring-relay-auth-key", args["relay_key"],
            "--monitoring-robot-receiver", args["receiver"],
            "--monitoring-robot-secret", args["route_secret"],
            "--monitoring-robot-endpoint-key", "url",
            "--monitoring-robot-signing-key", args["signing_key"],
            "--monitoring-severity", args["severity"],
            "--monitoring-notification-language", args["notification_language"],
            "--monitoring-loki-endpoint", args["loki_endpoint"],
            "--monitoring-log-before-seconds", str(args["before_seconds"]),
            "--monitoring-log-after-seconds", str(args["after_seconds"]),
            "--monitoring-log-max-lines", str(args["max_lines"]),
            "--monitoring-log-max-message-bytes", str(args["max_message_bytes"]),
            "--plan-file", str(plan_path), "--output-dir", str(plan_path.parent)]
    if args.get("grafana_url"):
        base.extend(("--monitoring-grafana-explore-url", args["grafana_url"],
                     "--monitoring-loki-datasource-uid", args["datasource_uid"],
                     "--monitoring-grafana-org-id", str(args["grafana_org_id"])))
    if args["kind"] == "email":
        base.extend(("--monitoring-smtp-host", args["smtp_host"],
                     "--monitoring-smtp-port", str(args["smtp_port"]),
                     "--monitoring-email-receiver", args["receiver"],
                     "--monitoring-email-sender", args["sender"],
                     "--monitoring-email-recipient", args["recipient"],
                     "--monitoring-email-username", args["username"],
                     "--monitoring-email-secret", args["route_secret"],
                     "--monitoring-email-secret-key", "password"))
    else:
        base.extend(("--monitoring-robot-kind", args["kind"],))
    run(base)
    try:
        record = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_id = record["id"]
    except (OSError, KeyError, ValueError, TypeError):
        raise RuntimeError(tr(lang, "phase03.monitoring.setup_plan_unreadable")) from None
    confirmation = ask(lang, "phase03.monitoring.setup_confirm_plan")
    if confirmation != plan_id:
        raise InterruptedError(tr(lang, "phase03.monitoring.setup_cancelled"))
    command = list(base)
    command[command.index("--change-plan")] = "--change-apply"
    command[command.index("--operation"):command.index("--operation") + 2] = []
    command.extend(("--confirm", plan_id))
    state["apply_started"] = True
    state.setdefault("uncertain", set()).update(secret_names)
    try:
        result = run(command, check=False)
        # A completed configuration plan intentionally returns 2 while its
        # delivery finding is UNVERIFIED. That is a successful apply requiring
        # a later alert-delivery E2E, not an uncertain Kubernetes write.
        if getattr(result, "returncode", 0) not in (0, 2):
            raise RuntimeError("monitoring route apply failed")
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        raise RuntimeError(tr(lang, "phase03.monitoring.setup_apply_uncertain")) from None
    return True


def configure_route(lang, namespace, alertmanager, version, release, relay_image,
                    grafana_url, datasource_uid, org_id):
    channel = ask(lang, "phase03.monitoring.setup_channel", "1")
    kinds = {"1": "email", "2": "wecom", "3": "dingtalk", "4": "feishu"}
    if channel not in kinds:
        raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_choice"))
    kind = kinds[channel]
    receiver = ask(lang, "phase03.monitoring.setup_receiver", "atlas-" + kind)
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", receiver) or len(receiver) > 63:
        raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_receiver"))
    language = ask(lang, "phase03.monitoring.setup_notification_language", "zh_CN")
    if language not in ("zh_CN", "zh_TW", "en"):
        raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_choice"))
    severity = ask(lang, "phase03.monitoring.setup_severity", "warning")
    if severity not in ("warning", "critical", "info"):
        raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_choice"))
    grafana_url = grafana_url or ask(lang, "phase03.monitoring.setup_grafana_url", "")
    if grafana_url:
        try:
            log_diagnostics.validate_settings({"loki_endpoint": "http://loki.logging.svc.cluster.local:3100",
                                               "grafana_explore_url": grafana_url,
                                               "loki_datasource_uid": datasource_uid or "loki",
                                               "grafana_org_id": org_id})
        except (TypeError, ValueError):
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_grafana_url")) from None
        datasource_uid = datasource_uid or ask(lang, "phase03.monitoring.setup_datasource_uid", "loki")
    if kind == "email":
        receiver = ask(lang, "phase03.monitoring.setup_email_receiver", receiver)
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", receiver) or len(receiver) > 63:
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_receiver"))
        smtp_host = ask(lang, "phase03.monitoring.setup_smtp_host")
        smtp_port = ask(lang, "phase03.monitoring.setup_smtp_port", "465")
        if not smtp_port.isdecimal() or not 1 <= int(smtp_port) <= 65535:
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_smtp_port"))
        sender = ask(lang, "phase03.monitoring.setup_sender")
        recipient = ask(lang, "phase03.monitoring.setup_recipient")
        username = ask(lang, "phase03.monitoring.setup_username", sender)
        password = ask(lang, "phase03.monitoring.setup_password", secret=True)
        if not password:
            raise ValueError(tr(lang, "phase03.monitoring.setup_empty_secret"))
        secret_name, secret_values, key = unique_name("atlas-smtp"), {"password": password}, "password"
        operation = "monitoring-email-diagnostics"
        signing_key = "-"
        extra = {"smtp_host": smtp_host, "smtp_port": int(smtp_port), "sender": sender,
                 "recipient": recipient, "username": username}
    else:
        webhook = ask(lang, "phase03.monitoring.setup_webhook", secret=True)
        try:
            validate_webhook(kind, webhook)
        except ValueError:
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_webhook"))
        secret_values = {"url": webhook}
        signing_key = "-"
        if kind in ("dingtalk", "feishu"):
            signing_key = "secret"
            secret_values[signing_key] = ask(lang, "phase03.monitoring.setup_signing_secret", secret=True)
            if not secret_values[signing_key]:
                raise ValueError(tr(lang, "phase03.monitoring.setup_empty_secret"))
        secret_name, key, operation, extra = unique_name("atlas-webhook"), "url", "monitoring-robot", {}
    if kind == "email":
        # The relay route currently supports one route per operation, while
        # configuration data merges routes without replacing existing ones.
        relay_kind = "email"
    else:
        relay_kind = kind
    secret_values = dict(secret_values)
    args = {"namespace": namespace, "release": release, "alertmanager": alertmanager,
            "alertmanager_version": version, "relay_image": relay_image,
            "route_secret": secret_name, "receiver": receiver, "signing_key": signing_key,
            "kind": relay_kind, "severity": severity, "notification_language": language,
            "loki_endpoint": "http://loki.logging.svc.cluster.local:3100",
            "before_seconds": 300, "after_seconds": 60, "max_lines": 20,
            "max_message_bytes": 8192, "grafana_url": grafana_url,
            "datasource_uid": datasource_uid, "grafana_org_id": org_id, **extra}
    return operation, args, secret_name, key, secret_values


def main(argv=None):
    parser = argparse.ArgumentParser(description="Guided first-time monitoring notification setup")
    parser.add_argument("--lang", choices=("en", "zh_CN", "zh_TW"), default="en")
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--require-complete", action="store_true",
                         help="return a nonzero status when route setup is cancelled")
    values = parser.parse_args(argv)
    lang, namespace = values.lang, values.namespace
    created = []
    preserved = set()
    state = {"apply_started": False, "uncertain": set()}
    try:
        if os.pathsep in os.environ.get("KUBECONFIG", ""):
            raise ValueError(tr(lang, "phase03.monitoring.setup_kubeconfig_multiple"))
        verify_cluster_configuration(lang)
        try:
            topology = monitoring_topology_mode(namespace)
        except ValueError:
            raise ValueError(tr(lang, "phase03.monitoring.setup_topology_lookup_failed")) from None
        if topology == "agent":
            raise ValueError(tr(lang, "ops.monitoring.center_resources_require_center"))
        if subprocess.run(["helm", "status", "kube-prometheus-stack", "-n", namespace],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError(tr(lang, "phase03.monitoring.setup_requires_prometheus"))
        if subprocess.run(["helm", "status", "loki", "-n", "logging"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError(tr(lang, "phase03.monitoring.setup_requires_loki"))
        if subprocess.run(["helm", "status", "alloy", "-n", "logging"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError(tr(lang, "phase03.monitoring.setup_requires_alloy"))
        alertmanager, version, release = alertmanager_identity(namespace, lang)
        ui = tr(lang, "phase03.monitoring.setup_intro", alertmanager, version, release)
        print(ui)
        image = relay_publication.runtime_image(ask(lang, "phase03.monitoring.setup_relay_image"))
        try:
            relay_publication.container(image, "atlas-relay-auth", "token", [
                {"receiver": "setup-check", "kind": "wecom", "language": "zh_CN",
                 "secretName": "atlas-route-secret", "endpointKey": "url", "secretKey": None}])
        except (TypeError, ValueError):
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_relay_image")) from None
        grafana_url = ask(lang, "phase03.monitoring.setup_grafana_url", "")
        datasource_uid = ask(lang, "phase03.monitoring.setup_datasource_uid", "loki") if grafana_url else ""
        org_id = ask(lang, "phase03.monitoring.setup_grafana_org_id", "1") if grafana_url else "1"
        if not org_id.isdecimal() or int(org_id) < 1:
            raise ValueError(tr(lang, "phase03.monitoring.setup_invalid_choice"))
        while True:
            operation, args, secret_name, key, secret_values = configure_route(
                lang, namespace, alertmanager, version, release, image,
                grafana_url, datasource_uid, int(org_id))
            receiver = args["receiver"]
            client = Kubernetes(os.environ.get("KUBECONFIG", "/etc/kubernetes/admin.conf"))
            manager = client.get(relay_publication.ALERTMANAGER, alertmanager, namespace)
            auth, previous_names = relay_publication.existing_credentials(manager["spec"], receiver)
            relay_secret, relay_key = auth if auth is not None else (unique_name("atlas-relay-auth"), "token")
            args.update(relay_secret=relay_secret, relay_key=relay_key)
            candidates = None
            try:
                candidates = notification_secrets.replacement_candidates(
                    client, namespace, receiver, previous_names - {relay_secret, secret_name})
            except (OperationError, OSError, ValueError, KeyError, TypeError):
                pass
            temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
            with tempfile.TemporaryDirectory(prefix="atlas-monitoring-plan-", dir=temp_root) as directory:
                plan_path = Path(directory) / "plan.json"
                print(tr(lang, "phase03.monitoring.setup_plan_intro", secret_name, relay_secret))
                if ask(lang, "phase03.monitoring.setup_create_plan", "no").casefold() not in ("y", "yes", "是"):
                    raise InterruptedError(tr(lang, "phase03.monitoring.setup_cancelled"))
                new_secrets = []
                if auth is None:
                    create_secret(namespace, relay_secret, {relay_key: secrets.token_urlsafe(32)}, "relay-auth")
                    created.append(relay_secret)
                    new_secrets.append(relay_secret)
                create_secret(namespace, secret_name, secret_values, "route", receiver)
                created.append(secret_name)
                new_secrets.append(secret_name)
                plan_and_apply(lang, operation, args, plan_path, new_secrets, state)
                preserved.update(new_secrets)
                state["uncertain"].difference_update(new_secrets)
                try:
                    if candidates is None:
                        raise OperationError("ops.change.kubernetes")
                    cleanup = notification_secrets.cleanup_unreferenced(client, namespace, candidates)
                    if cleanup["removed"]:
                        print(tr(lang, "phase03.monitoring.setup_secret_cleanup_complete",
                                 ", ".join(cleanup["removed"])))
                    if cleanup["retained"]:
                        print(tr(lang, "phase03.monitoring.setup_secret_cleanup_retained",
                                 ", ".join(cleanup["retained"])), file=sys.stderr)
                except (OperationError, OSError, ValueError, KeyError, TypeError):
                    print(tr(lang, "phase03.monitoring.setup_secret_cleanup_unverified"), file=sys.stderr)
            print(tr(lang, "phase03.monitoring.setup_route_complete", receiver))
            if ask(lang, "phase03.monitoring.setup_add_another", "no").casefold() not in ("y", "yes", "是"):
                break
        return 0
    except InterruptedError as error:
        print(str(error), file=sys.stderr)
        return 4 if values.require_complete else 0
    except KeyboardInterrupt:
        print(tr(lang, "phase03.monitoring.setup_cancelled"), file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        # Child tool diagnostics are intentionally not echoed: secret data is
        # accepted only through hidden input and Kubernetes Secret stdin.
        message = str(error) if isinstance(error, ValueError) else tr(lang, "phase03.monitoring.setup_failed")
        print(message, file=sys.stderr)
        if state["apply_started"]:
            print(tr(lang, "phase03.monitoring.setup_secret_preserved", ",".join(created)), file=sys.stderr)
        return 1
    finally:
        if created:
            for name in created:
                if name not in preserved and name not in state.get("uncertain", set()):
                    delete_secret(namespace, name)


if __name__ == "__main__":
    raise SystemExit(main())
