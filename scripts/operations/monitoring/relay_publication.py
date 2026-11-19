# SPDX-License-Identifier: MIT
"""Render Alertmanager sidecar and route resources for authenticated robot delivery."""
import hashlib
import json
import re
import copy
import base64
import io
import os
import pwd
import json
import zipfile
from pathlib import Path

from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from . import alert_routing
from .rules import _DNS_LABEL

CONFIG_NAME = "atlas-notification-relay"
CODE_CONFIG_PREFIX = "atlas-notification-relay-code-"
CONTAINER_NAME = "atlas-notification-relay"
VOLUME_NAME = "atlas-notification-relay-config"
CODE_VOLUME_NAME = "atlas-notification-relay-code"
CODE_MOUNT_PATH = "/opt/atlas-relay-code"
PORT = 18080
METRICS_PORT = 18081
METRICS_PORT_NAME = "atlas-metrics"
OWNER = "atlas-kubekit.io/managed-by"
SPEC_HASH = "atlas-kubekit.io/spec-sha256"
OWNER_VALUE = "monitoring"
OWNER_ENV = "ATLAS_RELAY_MANAGED"
OWNER_MARKER = "atlas-kubekit.io/managed-by=monitoring"
METRICS_POD_LABEL = "atlas-kubekit.io/relay-metrics"
METRICS_POD_LABEL_VALUE = "enabled"
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_-]{0,250}@sha256:[a-f0-9]{64}\Z")
DEFAULT_IMAGE = "python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
MAX_CODE_CONFIG_BYTES = 900 * 1024
ALERTMANAGER = "alertmanagers.monitoring.coreos.com"
ALERTMANAGER_CONFIG = "alertmanagerconfigs.monitoring.coreos.com"
CONFIG_MAP = "configmap"


def runtime_image(override=None):
    """Resolve the pinned default through the operator's selected image mirror."""
    if override:
        if not isinstance(override, str) or not IMAGE.fullmatch(override):
            raise ValueError("relay image must be pinned by sha256 digest")
        return override
    source_path = os.environ.get("ATLAS_ADDON_SOURCE_CONFIG")
    if not source_path:
        user = os.environ.get("SUDO_USER")
        home = Path(pwd.getpwnam(user).pw_dir) if user else Path.home()
        source_path = str(home / ".k8s-deploy" / "addon-sources.json")
    source = {"profile": "official"}
    try:
        data = json.loads(Path(source_path).read_text(encoding="utf-8"))
        from scripts.addons.sources import validate, registry_ref
        source = validate(data)
        return registry_ref(DEFAULT_IMAGE, source)
    except FileNotFoundError:
        return DEFAULT_IMAGE
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid add-on image source configuration") from None


def _receiver(value):
    if not isinstance(value, str) or not _DNS_LABEL.fullmatch(value):
        raise ValueError("invalid Alertmanager receiver")
    return value


def _route(value):
    common = {"receiver", "kind", "language", "secretName"}
    if not isinstance(value, dict) or not common <= set(value):
        raise ValueError("invalid relay route")
    receiver = _receiver(value["receiver"])
    kind = value["kind"]
    if kind not in ("wecom", "dingtalk", "feishu", "email") or value["language"] not in ("en", "zh_CN", "zh_TW"):
        raise ValueError("invalid relay route")
    secret = resource_name(value["secretName"])
    key_pattern = r"[A-Za-z0-9._-]{1,253}\Z"
    if kind == "email":
        expected = common | {"passwordKey", "smtpHost", "smtpPort", "sender", "recipient", "username"}
        if set(value) != expected or not isinstance(value["passwordKey"], str) or not re.fullmatch(key_pattern, value["passwordKey"]):
            raise ValueError("invalid relay email route")
        from .channels.smtp import _settings
        smtp = _settings({"host": value["smtpHost"], "port": value["smtpPort"], "sender": value["sender"],
                          "recipient": value["recipient"], "username": value["username"], "language": value["language"]})
        return {"receiver": receiver, "kind": kind, "language": value["language"], "secretName": secret,
                "passwordKey": value["passwordKey"], "smtpHost": smtp["host"], "smtpPort": smtp["port"],
                "sender": smtp["sender"], "recipient": smtp["recipient"], "username": smtp["username"]}
    expected = common | {"endpointKey", "secretKey"}
    if set(value) != expected:
        raise ValueError("invalid relay route")
    endpoint_key, signing_key = value["endpointKey"], value["secretKey"]
    if not isinstance(endpoint_key, str) or not re.fullmatch(key_pattern, endpoint_key):
        raise ValueError("invalid relay route")
    if signing_key is not None and (not isinstance(signing_key, str) or not re.fullmatch(key_pattern, signing_key)):
        raise ValueError("invalid relay route")
    if kind in ("dingtalk", "feishu") and not signing_key:
        raise ValueError("signing key required")
    return {"receiver": receiver, "kind": kind, "language": value["language"],
            "secretName": secret, "endpointKey": endpoint_key, "secretKey": signing_key}


def _route_environment_names(route):
    if route["kind"] == "email":
        return None, _password_env(route["receiver"])
    return _env_names(route["receiver"], route["secretKey"] is not None)


def _password_env(receiver):
    return "ATLAS_RELAY_PASSWORD_" + hashlib.sha256(receiver.encode()).hexdigest()[:12].upper()


def _receiver_environment_names(receiver):
    return {*_env_names(receiver, True), _password_env(receiver)}


def route_config(routes, diagnostics=None):
    """Produce non-secret JSON metadata; secret values remain in Kubernetes Secrets."""
    if not isinstance(routes, (list, tuple)) or not 1 <= len(routes) <= 100:
        raise ValueError("invalid relay routes")
    seen, rendered = set(), []
    for raw in routes:
        route = _route(raw)
        if route["receiver"] in seen:
            raise ValueError("duplicate relay receiver")
        seen.add(route["receiver"])
        endpoint_env, secret_env = _route_environment_names(route)
        item = {"receiver": route["receiver"], "kind": route["kind"], "language": route["language"]}
        if route["kind"] == "email":
            item["password_env"] = secret_env
            item["smtp"] = {"host": route["smtpHost"], "port": route["smtpPort"], "sender": route["sender"],
                            "recipient": route["recipient"], "username": route["username"],
                            "language": route["language"]}
        else:
            item["endpoint_env"] = endpoint_env
            if secret_env:
                item["secret_env"] = secret_env
        rendered.append(item)
    rendered.sort(key=lambda item: item["receiver"])
    body = {"version": 1, "routes": rendered}
    if diagnostics is not None:
        from .log_diagnostics import validate_settings
        try:
            body["diagnostics"] = validate_settings(diagnostics)
        except (TypeError, ValueError):
            raise ValueError("invalid relay diagnostics") from None
    return body


def _env_names(receiver, has_secret):
    suffix = hashlib.sha256(receiver.encode()).hexdigest()[:12].upper()
    return "ATLAS_RELAY_ENDPOINT_" + suffix, ("ATLAS_RELAY_SECRET_" + suffix if has_secret else None)


def merge_route_config(existing, route, diagnostics=None):
    """Merge one route into the owned metadata while preserving other receivers."""
    route = _route(route)
    if existing is None:
        current = {"version": 1, "routes": []}
    else:
        if (not isinstance(existing, dict) or set(existing) - {"version", "routes", "diagnostics"} or
                not {"version", "routes"} <= set(existing) or
                type(existing["version"]) is not int or existing["version"] != 1 or
                not isinstance(existing["routes"], list) or not 0 <= len(existing["routes"]) <= 100):
            raise ValueError("invalid existing relay routes")
        current = {"version": 1, "routes": []}
        if "diagnostics" in existing:
            from .log_diagnostics import validate_settings
            try:
                current["diagnostics"] = validate_settings(existing["diagnostics"])
            except (TypeError, ValueError):
                raise ValueError("invalid existing relay diagnostics") from None
        seen = set()
        for item in existing["routes"]:
            if not isinstance(item, dict) or not {"receiver", "kind", "language"} <= set(item):
                raise ValueError("invalid existing relay route")
            receiver = _receiver(item["receiver"])
            if receiver in seen:
                raise ValueError("duplicate existing relay route")
            seen.add(receiver)
            if item["kind"] == "email":
                expected = {"receiver", "kind", "language", "password_env", "smtp"}
                if set(item) != expected or not isinstance(item["smtp"], dict):
                    raise ValueError("invalid existing relay email route")
                if item["password_env"] != _password_env(receiver):
                    raise ValueError("invalid existing relay email route")
                from .channels.smtp import _settings
                smtp = _settings(item["smtp"])
                if smtp["language"] != item["language"]:
                    raise ValueError("invalid existing relay email route")
                current["routes"].append(dict(item, smtp=smtp))
                continue
            expected = {"receiver", "kind", "language", "endpoint_env"}
            if set(item) - (expected | {"secret_env"}) or not expected <= set(item):
                raise ValueError("invalid existing relay route")
            endpoint_env, secret_env = _env_names(receiver, "secret_env" in item)
            if (item["kind"] not in ("wecom", "dingtalk", "feishu") or
                    item["language"] not in ("en", "zh_CN", "zh_TW") or item["endpoint_env"] != endpoint_env or
                    ("secret_env" in item and item["secret_env"] != secret_env) or
                    (item["kind"] in ("dingtalk", "feishu") and "secret_env" not in item)):
                raise ValueError("invalid existing relay route")
            current["routes"].append(dict(item))
    endpoint_env, secret_env = _route_environment_names(route)
    if route["kind"] == "email":
        rendered = {"receiver": route["receiver"], "kind": route["kind"], "language": route["language"],
                    "password_env": secret_env,
                    "smtp": {"host": route["smtpHost"], "port": route["smtpPort"], "sender": route["sender"],
                             "recipient": route["recipient"], "username": route["username"],
                             "language": route["language"]}}
    else:
        rendered = {"receiver": route["receiver"], "kind": route["kind"], "language": route["language"],
                    "endpoint_env": endpoint_env}
        if secret_env:
            rendered["secret_env"] = secret_env
    if len(current["routes"]) >= 100 and not any(
            item["receiver"] == route["receiver"] for item in current["routes"]):
        raise ValueError("too many relay routes")
    current["routes"] = [item for item in current["routes"] if item["receiver"] != route["receiver"]]
    current["routes"].append(rendered)
    current["routes"].sort(key=lambda item: item["receiver"])
    if diagnostics is not None:
        from .log_diagnostics import validate_settings
        try:
            current["diagnostics"] = validate_settings(diagnostics)
        except (TypeError, ValueError):
            raise ValueError("invalid relay diagnostics") from None
    return current


def route_environment(route):
    """SecretKeyRef-backed environment entries for one route."""
    route = _route(route)
    endpoint_env, secret_env = _route_environment_names(route)
    if route["kind"] == "email":
        return [{"name": secret_env, "valueFrom": {"secretKeyRef": {
            "name": route["secretName"], "key": route["passwordKey"]}}}]
    result = [{"name": endpoint_env, "valueFrom": {"secretKeyRef": {
        "name": route["secretName"], "key": route["endpointKey"]}}}]
    if secret_env:
        result.append({"name": secret_env, "valueFrom": {"secretKeyRef": {
            "name": route["secretName"], "key": route["secretKey"]}}})
    return result


def _owned_environment(existing, route):
    """Keep other previously managed route references without accepting literal secrets."""
    if existing is None:
        return []
    entries = existing.get("env", [])
    if not isinstance(entries, list):
        raise ValueError("invalid existing relay environment")
    replaced = (_receiver_environment_names(route["receiver"]) |
                {"ATLAS_RELAY_TOKEN", OWNER_ENV, "ATLAS_LOCALES_DIR", "PYTHONDONTWRITEBYTECODE"})
    result = []
    seen = set()
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or
                item["name"] in seen):
            raise ValueError("invalid existing relay environment")
        seen.add(item["name"])
        if item["name"] in replaced:
            continue
        reference = item.get("valueFrom", {}).get("secretKeyRef") if isinstance(item.get("valueFrom"), dict) else None
        if (set(item) != {"name", "valueFrom"} or not isinstance(reference, dict) or
                set(reference) - {"name", "key", "optional"} or
                not isinstance(reference.get("name"), str) or not isinstance(reference.get("key"), str)):
            raise ValueError("unsafe existing relay environment")
        result.append(dict(item))
    return result


def config_map(namespace, release, routes, diagnostics=None):
    if not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError("invalid relay namespace")
    if not isinstance(release, str) or not _DNS_LABEL.fullmatch(release):
        raise ValueError("invalid relay release")
    body = route_config(routes, diagnostics)
    return config_map_data(namespace, release, body)


def config_map_data(namespace, release, body):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or not _DNS_LABEL.fullmatch(release) or
            not isinstance(body, dict) or set(body) - {"version", "routes", "diagnostics"} or
            not {"version", "routes"} <= set(body) or body.get("version") != 1):
        raise ValueError("invalid relay config map")
    body = copy.deepcopy(body)
    if "diagnostics" in body:
        from .log_diagnostics import validate_settings
        try:
            body["diagnostics"] = validate_settings(body["diagnostics"])
        except (TypeError, ValueError):
            raise ValueError("invalid relay diagnostics") from None
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIG_NAME, "namespace": namespace,
                         "labels": {"release": release, OWNER: OWNER_VALUE}},
            "data": {"routes.json": json.dumps(body, sort_keys=True, separators=(",", ":"))}}


def _relay_archive():
    """Build a deterministic, standard-library-only zipapp from the relay sources."""
    root = Path(__file__).resolve().parents[3]
    files = ["scripts/lib/__init__.py", "scripts/lib/i18n.py",
             "scripts/operations/__init__.py", "scripts/operations/monitoring/__init__.py",
             "scripts/operations/monitoring/log_diagnostics.py", "scripts/operations/monitoring/loki.py"]
    files.extend(str(path.relative_to(root)) for path in sorted(
        (root / "scripts/operations/monitoring/channels").glob("*.py")))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = [("__main__.py", b"from scripts.operations.monitoring.channels.relay import main\n\nmain()\n"),
                   ("scripts/__init__.py", b"")]
        entries.extend((name, (root / name).read_bytes()) for name in files)
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o444 << 16
            archive.writestr(info, content)
    return output.getvalue()


def code_config_map(namespace, release):
    """Publish immutable, content-addressed relay code and locale catalogs."""
    if not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError("invalid relay namespace")
    if not isinstance(release, str) or not _DNS_LABEL.fullmatch(release):
        raise ValueError("invalid relay release")
    archive = _relay_archive()
    root = Path(__file__).resolve().parents[3]
    catalogs = {name: (root / "locales" / name).read_text(encoding="utf-8")
                for name in ("en.json", "zh_cn.json", "zh_tw.json")}
    body = {"binaryData": {"relay.pyz": base64.b64encode(archive).decode("ascii")}, "data": catalogs}
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_CODE_CONFIG_BYTES:
        raise ValueError("relay code ConfigMap exceeds safe size limit")
    digest = hashlib.sha256(serialized).hexdigest()
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CODE_CONFIG_PREFIX + digest[:16], "namespace": namespace,
                         "labels": {"release": release, OWNER: OWNER_VALUE},
                         "annotations": {SPEC_HASH: digest}},
            "immutable": True, **body}


def container(image, auth_secret, auth_key, routes):
    image = DEFAULT_IMAGE if image in (None, "") else image
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("relay image must be pinned by sha256 digest")
    auth_secret = resource_name(auth_secret)
    if not isinstance(auth_key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", auth_key):
        raise ValueError("invalid relay authorization secret key")
    normalized = [_route(item) for item in routes]
    route_data = route_config(normalized)["routes"]
    env = [{"name": "ATLAS_RELAY_TOKEN", "valueFrom": {"secretKeyRef": {"name": auth_secret, "key": auth_key}}},
           {"name": OWNER_ENV, "value": OWNER_MARKER}]
    for route in sorted(normalized, key=lambda item: item["receiver"]):
        env.extend(route_environment(route))
    return {"name": CONTAINER_NAME, "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["python", CODE_MOUNT_PATH + "/relay.pyz"],
            "args": ["--port", str(PORT), "--metrics-address", "0.0.0.0",
                     "--metrics-port", str(METRICS_PORT), "--routes-file", "/etc/atlas-relay/routes.json"],
            "env": env + [{"name": "ATLAS_LOCALES_DIR", "value": CODE_MOUNT_PATH + "/locales"},
                          {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}], "ports": [
                {"name": "atlas-relay", "containerPort": PORT, "protocol": "TCP"},
                {"name": METRICS_PORT_NAME, "containerPort": METRICS_PORT, "protocol": "TCP"}],
            "volumeMounts": [{"name": VOLUME_NAME, "mountPath": "/etc/atlas-relay", "readOnly": True},
                             {"name": CODE_VOLUME_NAME, "mountPath": CODE_MOUNT_PATH, "readOnly": True}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "25m", "memory": "48Mi"},
                          "limits": {"cpu": "250m", "memory": "256Mi"}},
            "readinessProbe": {"exec": {"command": ["python3", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:" +
                str(PORT) + "/-/healthy', timeout=2)"]},
                "initialDelaySeconds": 2, "periodSeconds": 5, "timeoutSeconds": 3},
            "livenessProbe": {"exec": {"command": ["python3", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:" +
                str(PORT) + "/-/healthy', timeout=2)"]},
                "initialDelaySeconds": 5, "periodSeconds": 10, "timeoutSeconds": 3}}


def merge_container(image, auth_secret, auth_key, route, existing=None):
    value = container(image, auth_secret, auth_key, [route])
    value["env"] = _owned_environment(existing, route) + value["env"]
    return value


def volume():
    return {"name": VOLUME_NAME, "configMap": {"name": CONFIG_NAME, "defaultMode": 0o444}}


def code_volume(config_name):
    if (not isinstance(config_name, str) or
            not re.fullmatch(re.escape(CODE_CONFIG_PREFIX) + r"[a-f0-9]{16}", config_name)):
        raise ValueError("invalid relay code ConfigMap name")
    return {"name": CODE_VOLUME_NAME, "configMap": {"name": config_name, "defaultMode": 0o444,
            "items": [{"key": "relay.pyz", "path": "relay.pyz"},
                      {"key": "en.json", "path": "locales/en.json"},
                      {"key": "zh_cn.json", "path": "locales/zh_cn.json"},
                      {"key": "zh_tw.json", "path": "locales/zh_tw.json"}]}}


def alertmanager_config(namespace, release, receiver, severity, auth_secret, auth_key, route_policy=None):
    if not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError("invalid relay namespace")
    if not isinstance(release, str) or not _DNS_LABEL.fullmatch(release):
        raise ValueError("invalid relay release")
    receiver = _receiver(receiver)
    if severity not in ("warning", "critical", "info"):
        raise ValueError("invalid relay severity")
    auth_secret = resource_name(auth_secret)
    if not isinstance(auth_key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", auth_key):
        raise ValueError("invalid relay authorization secret key")
    routing = alert_routing.route_policy() if route_policy is None else alert_routing.route_policy(
        route_policy.get("matchers"), route_policy.get("groupBy"), route_policy.get("groupWait"),
        route_policy.get("groupInterval"), route_policy.get("repeatInterval"))
    suffix = hashlib.sha256(receiver.encode()).hexdigest()[:10]
    name = ("atlas-robot-" + receiver)[:52].rstrip("-") + "-" + suffix
    return {"apiVersion": "monitoring.coreos.com/v1alpha1", "kind": "AlertmanagerConfig",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"release": release, OWNER: "monitoring"}},
            "spec": {"route": {"receiver": receiver, "continue": True,
                                **alert_routing.render_route(severity, routing)},
                     "receivers": [{"name": receiver, "webhookConfigs": [{
                         "url": "http://127.0.0.1:" + str(PORT) + "/v1/alert", "sendResolved": True,
                         "httpConfig": {"authorization": {"type": "Bearer",
                             "credentials": {"name": auth_secret, "key": auth_key}}}}]}]}}


def _metadata(value):
    try:
        meta = value["metadata"]
        uid, version = meta["uid"], meta["resourceVersion"]
        if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
            raise ValueError()
        return meta
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.kubernetes") from None


def _identity(value, field):
    if value is None:
        return None
    meta = _metadata(value)
    labels, annotations = meta.get("labels"), meta.get("annotations")
    content = value.get(field)
    if (not isinstance(content, dict) or not isinstance(labels, dict) or not isinstance(annotations, dict) or
            labels.get(OWNER) != OWNER_VALUE or annotations.get(SPEC_HASH) != fingerprint(content)):
        raise OperationError("ops.change.foreign_resource")
    return {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"], "specHash": fingerprint(content)}


def _code_identity(value):
    if value is None:
        return None
    meta = _metadata(value)
    body = {"binaryData": value.get("binaryData"), "data": value.get("data")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    labels, annotations = meta.get("labels"), meta.get("annotations")
    if (value.get("immutable") is not True or not isinstance(labels, dict) or
            labels.get(OWNER) != OWNER_VALUE or not isinstance(annotations, dict) or
            annotations.get(SPEC_HASH) != digest or meta.get("name") != CODE_CONFIG_PREFIX + digest[:16]):
        raise OperationError("ops.change.foreign_resource")
    return {"name": meta["name"], "uid": meta["uid"], "resourceVersion": meta["resourceVersion"],
            "specHash": digest}


def _manager_compatible(manager, namespace, release, version):
    meta = _metadata(manager)
    spec = manager.get("spec", {})
    if not isinstance(spec, dict):
        raise OperationError("ops.monitoring.robot_incompatible")
    selector = spec.get("alertmanagerConfigSelector")
    namespaces = spec.get("alertmanagerConfigNamespaceSelector")
    strategy_config = spec.get("alertmanagerConfigMatcherStrategy") or {}
    strategy = strategy_config.get("type", "OnNamespace") if isinstance(strategy_config, dict) else None
    labels = {"release": release, OWNER: OWNER_VALUE}
    image = spec.get("image")
    pod_metadata = spec.get("podMetadata") or {}
    pod_labels = pod_metadata.get("labels", {}) if isinstance(pod_metadata, dict) else None
    selected_labels = selector.get("matchLabels", {}) if isinstance(selector, dict) else None
    if (not isinstance(pod_labels, dict) or
            pod_labels.get(METRICS_POD_LABEL) not in (None, METRICS_POD_LABEL_VALUE)):
        raise OperationError("ops.monitoring.robot_incompatible")
    if (not isinstance(selector, dict) or set(selector) - {"matchLabels"} or
            not isinstance(selected_labels, dict) or selected_labels.get(OWNER) != OWNER_VALUE or
            any(k not in labels or labels[k] != v for k, v in selected_labels.items()) or
            namespaces not in (None, {}) or strategy not in ("None", "OnNamespaceExceptForAlertmanagerNamespace") or
            not isinstance(spec.get("version"), str) or spec["version"].lstrip("v") != version or
            (image is not None and (not isinstance(image, str) or not image.endswith("/alertmanager:v" + version)))):
        raise OperationError("ops.monitoring.robot_incompatible")
    _managed_sidecar(spec)
    return meta


def _managed_sidecar(spec):
    containers = spec.get("containers", [])
    volumes = spec.get("volumes", [])
    if not isinstance(containers, list) or not isinstance(volumes, list):
        raise OperationError("ops.monitoring.robot_incompatible")
    sidecars = [item for item in containers if isinstance(item, dict) and item.get("name") == CONTAINER_NAME]
    if len(sidecars) > 1:
        raise OperationError("ops.change.foreign_resource")
    existing = sidecars[0] if sidecars else None
    if existing is not None:
        marker = next((item.get("value") for item in existing.get("env", [])
                       if isinstance(item, dict) and item.get("name") == OWNER_ENV), None)
        if marker != OWNER_MARKER:
            raise OperationError("ops.change.foreign_resource")
        env = existing.get("env", [])
        if not isinstance(env, list):
            raise OperationError("ops.monitoring.robot_incompatible")
        names = set()
        for item in env:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item["name"] in names:
                raise OperationError("ops.monitoring.robot_incompatible")
            names.add(item["name"])
            if item["name"] in ("ATLAS_RELAY_TOKEN",) or item["name"].startswith(("ATLAS_RELAY_ENDPOINT_", "ATLAS_RELAY_SECRET_", "ATLAS_RELAY_PASSWORD_")):
                reference = item.get("valueFrom", {}).get("secretKeyRef") if isinstance(item.get("valueFrom"), dict) else None
                if (set(item) != {"name", "valueFrom"} or not isinstance(reference, dict) or
                        not isinstance(reference.get("name"), str) or not isinstance(reference.get("key"), str)):
                    raise OperationError("ops.monitoring.robot_incompatible")
    named_volumes = [item for item in volumes if isinstance(item, dict) and item.get("name") == VOLUME_NAME]
    if len(named_volumes) > 1:
        raise OperationError("ops.change.foreign_resource")
    if named_volumes and named_volumes[0] != volume():
        raise OperationError("ops.change.foreign_resource")
    code_volumes = [item for item in volumes if isinstance(item, dict) and item.get("name") == CODE_VOLUME_NAME]
    if len(code_volumes) > 1:
        raise OperationError("ops.change.foreign_resource")
    if code_volumes:
        try:
            code_name = code_volumes[0]["configMap"]["name"]
            if code_volumes[0] != code_volume(code_name):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise OperationError("ops.change.foreign_resource") from None
    return existing


def _route_document(config_map_value):
    if config_map_value is None:
        return None
    _identity(config_map_value, "data")
    try:
        value = json.loads(config_map_value["data"]["routes.json"])
        if not isinstance(value, dict):
            raise ValueError()
        # Validate the route document and generated env names before preserving it.
        validated = merge_route_config(value, {"receiver": "atlas-validation", "kind": "wecom", "language": "zh_CN",
                                               "secretName": "atlas-validation", "endpointKey": "endpoint", "secretKey": None})
        if "diagnostics" in validated:
            value["diagnostics"] = validated["diagnostics"]
        value["routes"] = [item for item in value["routes"] if item["receiver"] != "atlas-validation"]
        return value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise OperationError("ops.monitoring.robot_incompatible") from None


def existing_credentials(spec, receiver):
    """Read only the shared relay token and the selected receiver's Secret references."""
    sidecar = _managed_sidecar(spec)
    if sidecar is None:
        return None, frozenset()
    names = _receiver_environment_names(_receiver(receiver))
    references = {entry["name"]: entry["valueFrom"]["secretKeyRef"]
                  for entry in sidecar.get("env", [])
                  if entry["name"] == "ATLAS_RELAY_TOKEN" or entry["name"] in names}
    auth = references.pop("ATLAS_RELAY_TOKEN", None)
    if auth is None:
        raise OperationError("ops.monitoring.robot_incompatible")
    return (resource_name(auth["name"]), auth["key"]), frozenset(
        resource_name(reference["name"]) for reference in references.values())


def _credential_rows(client, namespace, values):
    route = values["route"]
    if route["kind"] == "email":
        references = [(values["authSecret"], values["authKey"]),
                      (route["secretName"], route["passwordKey"])]
    else:
        references = [(values["authSecret"], values["authKey"]),
                      (route["secretName"], route["endpointKey"])]
    if route["kind"] != "email" and route["secretKey"]:
        references.append((route["secretName"], route["secretKey"]))
    identities = {}
    for name, key in references:
        identity = client.secret_metadata(namespace, name, key)
        identities[(name, key)] = {"name": name, "key": key, **identity}
    return [identities[key] for key in sorted(identities)]


def capture(client, values):
    namespace = resource_name(values["namespace"])
    release = resource_name(values["release"])
    manager_name = resource_name(values["managerName"])
    route = _route(values["route"])
    desired_labels = {"release": release, OWNER: OWNER_VALUE}
    manager = client.get(ALERTMANAGER, manager_name, namespace)
    manager_meta = _manager_compatible(manager, namespace, release, values["version"])
    config_map_value = client.get(CONFIG_MAP, CONFIG_NAME, namespace, optional=True)
    code_manifest = code_config_map(namespace, release)
    code_map_value = client.get(CONFIG_MAP, code_manifest["metadata"]["name"], namespace, optional=True)
    current_routes = _route_document(config_map_value)
    sidecar = _managed_sidecar(manager["spec"])
    if current_routes and sidecar is not None:
        if any(item["receiver"] != route["receiver"] for item in current_routes["routes"]):
            current_auth, _ = existing_credentials(manager["spec"], route["receiver"])
            if current_auth != (values["authSecret"], values["authKey"]):
                raise OperationError("ops.monitoring.robot_incompatible")
        environment_names = {item["name"] for item in sidecar.get("env", []) if isinstance(item, dict)}
        for item in current_routes["routes"]:
            route_envs = ((item["password_env"],) if item["kind"] == "email" else
                          (item["endpoint_env"], item.get("secret_env")))
            if any(env_name and env_name not in environment_names for env_name in route_envs):
                raise OperationError("ops.monitoring.robot_incompatible")
    route_manifest = alertmanager_config(namespace, release, route["receiver"], values["severity"],
                                         values["authSecret"], values["authKey"], values.get("routePolicy"))
    existing_route = client.get(ALERTMANAGER_CONFIG, route_manifest["metadata"]["name"], namespace, optional=True)
    if existing_route is not None:
        if any(existing_route.get("metadata", {}).get("labels", {}).get(k) != v
               for k, v in desired_labels.items()):
            raise OperationError("ops.change.foreign_resource")
        _identity(existing_route, "spec")
    credentials = _credential_rows(client, namespace, values)
    return {"manager": {"uid": manager_meta["uid"], "specHash": fingerprint(manager["spec"])},
            "credentials": credentials,
            "configMap": _identity(config_map_value, "data"),
            "codeMap": _code_identity(code_map_value),
            "route": _identity(existing_route, "spec")}


def desired(values):
    route = _route(values["route"])
    endpoint_env, secret_env = _route_environment_names(route)
    route_document = {"receiver": route["receiver"], "kind": route["kind"], "language": route["language"]}
    if route["kind"] == "email":
        route_document.update({"password_env": secret_env,
                               "smtp": {"host": route["smtpHost"], "port": route["smtpPort"],
                                        "sender": route["sender"], "recipient": route["recipient"],
                                        "username": route["username"], "language": route["language"]}})
    else:
        route_document["endpoint_env"] = endpoint_env
        if secret_env:
            route_document["secret_env"] = secret_env
    body = {"version": 1, "routes": [
        route_document]}
    if values.get("diagnostics") is not None:
        body["diagnostics"] = values["diagnostics"]
    code_map = code_config_map(values["namespace"], values["release"])
    return {"configMap": config_map_data(values["namespace"], values["release"], body),
            "codeConfigMap": code_map,
            "container": container(values["image"], values["authSecret"], values["authKey"], [route]),
            "volume": volume(),
            "codeVolume": code_volume(code_map["metadata"]["name"]),
            "route": alertmanager_config(values["namespace"], values["release"], route["receiver"],
                                          values["severity"], values["authSecret"], values["authKey"],
                                          values.get("routePolicy"))}


def _write(client, current, desired_value, field, kind):
    value = copy.deepcopy(desired_value if current is None else current)
    if field == "spec":
        value["spec"] = copy.deepcopy(desired_value["spec"])
    else:
        value[field] = copy.deepcopy(desired_value[field])
    value.setdefault("metadata", {}).setdefault("labels", {}).update(desired_value["metadata"]["labels"])
    value["metadata"].setdefault("annotations", {})[SPEC_HASH] = fingerprint(value[field])
    result = client.create(value) if current is None else client.replace(value)
    if result is None:
        raise OperationError("ops.change.stale")
    return result


def publish(client, values):
    """Write ConfigMap, Alertmanager sidecar and selected route in guarded order."""
    expected = values["expected"]
    if capture(client, values) != expected:
        raise OperationError("ops.change.stale")
    namespace = values["namespace"]
    manager_name = values["managerName"]
    route = _route(values["route"])
    map_manifest = desired(values)["configMap"]
    code_manifest = desired(values)["codeConfigMap"]
    manager = client.get(ALERTMANAGER, manager_name, namespace)
    current_map = client.get(CONFIG_MAP, CONFIG_NAME, namespace, optional=True)
    current_code_map = client.get(CONFIG_MAP, code_manifest["metadata"]["name"], namespace, optional=True)
    current_routes = _route_document(current_map)
    body = merge_route_config(current_routes, route, values.get("diagnostics"))
    map_manifest = config_map_data(namespace, values["release"], body)
    map_manifest["metadata"].setdefault("annotations", {})[SPEC_HASH] = fingerprint(map_manifest["data"])
    route_manifest = desired(values)["route"]
    current_route = client.get(ALERTMANAGER_CONFIG, route_manifest["metadata"]["name"], namespace,
                               optional=True)
    written = []
    try:
        if current_code_map is None:
            if client.create(code_manifest) is None:
                raise OperationError("ops.change.stale")
            written.append("relay code ConfigMap")
        _write(client, current_map, map_manifest, "data", CONFIG_MAP)
        written.append("ConfigMap")
        desired_container = merge_container(values["image"], values["authSecret"], values["authKey"],
                                            route, _managed_sidecar(manager["spec"]))
        desired_spec = copy.deepcopy(manager["spec"])
        containers = list(desired_spec.get("containers", []))
        existing_index = next((i for i, item in enumerate(containers)
                               if item.get("name") == CONTAINER_NAME), None)
        if existing_index is None:
            containers.append(desired_container)
        else:
            containers[existing_index] = desired_container
        desired_spec["containers"] = containers
        volumes = list(desired_spec.get("volumes", []))
        if not any(item.get("name") == VOLUME_NAME for item in volumes):
            volumes.append(volume())
        desired_code_volume = code_volume(code_manifest["metadata"]["name"])
        volumes = [item for item in volumes if item.get("name") != CODE_VOLUME_NAME]
        volumes.append(desired_code_volume)
        desired_spec["volumes"] = volumes
        pod_metadata = desired_spec.setdefault("podMetadata", {})
        if not isinstance(pod_metadata, dict):
            raise OperationError("ops.monitoring.robot_incompatible")
        labels = pod_metadata.setdefault("labels", {})
        if not isinstance(labels, dict) or labels.get(METRICS_POD_LABEL) not in (None, METRICS_POD_LABEL_VALUE):
            raise OperationError("ops.monitoring.robot_incompatible")
        labels[METRICS_POD_LABEL] = METRICS_POD_LABEL_VALUE
        annotations = pod_metadata.setdefault("annotations", {})
        if not isinstance(annotations, dict):
            raise OperationError("ops.monitoring.robot_incompatible")
        annotations["atlas-kubekit.io/relay-config-sha256"] = fingerprint(body)
        updated_manager = copy.deepcopy(manager)
        updated_manager["spec"] = desired_spec
        manager_result = client.replace(updated_manager)
        if manager_result is None:
            raise OperationError("ops.change.stale")
        written.append("Alertmanager")
        _write(client, current_route, route_manifest, "spec", ALERTMANAGER_CONFIG)
        written.append("AlertmanagerConfig")
    except Exception as error:
        if written:
            raise OperationError("ops.monitoring.robot_partial", ", ".join(written)) from error
        raise
    after = capture(client, values)
    if (after["route"] is None or after["route"]["specHash"] != fingerprint(route_manifest["spec"]) or
            after["configMap"] is None or after["configMap"]["specHash"] != fingerprint(map_manifest["data"]) or
            after["codeMap"] is None or after["codeMap"]["specHash"] != code_manifest["metadata"]["annotations"][SPEC_HASH] or
            after["manager"]["uid"] != expected["manager"]["uid"] or
            after["credentials"] != expected["credentials"]):
        raise OperationError("ops.change.stale")
    return {"state": "resources-written", "manager": after["manager"],
            "configMap": after["configMap"], "route": after["route"], "delivery": "unverified"}


def preflight(client, values):
    """Ask the API server to validate all three resources before creating a plan."""
    if not callable(getattr(client, "call", None)):
        return
    expected = values["expected"]
    if capture(client, values) != expected:
        raise OperationError("ops.change.stale")
    rendered = desired(values)
    namespace = values["namespace"]
    current_map = client.get(CONFIG_MAP, CONFIG_NAME, namespace, optional=True)
    code_manifest = rendered["codeConfigMap"]
    current_code_map = client.get(CONFIG_MAP, code_manifest["metadata"]["name"], namespace, optional=True)
    body = merge_route_config(_route_document(current_map), values["route"], values.get("diagnostics"))
    cm = config_map_data(namespace, values["release"], body)
    cm["metadata"].setdefault("annotations", {})[SPEC_HASH] = fingerprint(cm["data"])
    manager = client.get(ALERTMANAGER, values["managerName"], namespace)
    spec = copy.deepcopy(manager["spec"])
    containers = list(spec.get("containers", []))
    sidecar = merge_container(values["image"], values["authSecret"], values["authKey"],
                              values["route"], _managed_sidecar(spec))
    index = next((i for i, item in enumerate(containers) if item.get("name") == CONTAINER_NAME), None)
    if index is None:
        containers.append(sidecar)
    else:
        containers[index] = sidecar
    spec["containers"] = containers
    volumes = list(spec.get("volumes", []))
    if not any(item.get("name") == VOLUME_NAME for item in volumes):
        volumes.append(volume())
    volumes = [item for item in volumes if item.get("name") != CODE_VOLUME_NAME]
    volumes.append(code_volume(code_manifest["metadata"]["name"]))
    spec["volumes"] = volumes
    pod_metadata = spec.setdefault("podMetadata", {})
    if not isinstance(pod_metadata, dict):
        raise OperationError("ops.monitoring.robot_incompatible")
    pod_annotations = pod_metadata.setdefault("annotations", {})
    if not isinstance(pod_annotations, dict):
        raise OperationError("ops.monitoring.robot_incompatible")
    pod_annotations["atlas-kubekit.io/relay-config-sha256"] = fingerprint(body)
    manager_copy = copy.deepcopy(manager)
    manager_copy["spec"] = spec
    current_route = client.get(ALERTMANAGER_CONFIG, rendered["route"]["metadata"]["name"],
                               namespace, optional=True)
    resources = [(ALERTMANAGER, manager, manager_copy),
                 (ALERTMANAGER_CONFIG, current_route, rendered["route"])]
    if current_code_map is None:
        resources.insert(0, (CONFIG_MAP, None, code_manifest))
    resources.insert(1 if current_code_map is None else 0, (CONFIG_MAP, current_map, cm))
    for kind, current, manifest in resources:
        operation = "replace" if current is not None else "create"
        if current is not None:
            current_meta = _metadata(current)
            metadata = manifest.setdefault("metadata", {})
            metadata["resourceVersion"] = current_meta["resourceVersion"]
            metadata["uid"] = current_meta["uid"]
        result = client.call([operation, "--dry-run=server", "-f", "-", "-o", "json"], value=manifest)
        if result is None:
            raise OperationError("ops.monitoring.robot_incompatible")
