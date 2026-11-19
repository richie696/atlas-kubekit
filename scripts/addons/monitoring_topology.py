# SPDX-License-Identifier: MIT
"""Render the explicit independent-center and Prometheus Agent configurations."""
import re
import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.addons.monitoring_values import safe_monitoring_values
from scripts.addons.sources import manifest_loader, yaml_module

_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def _https_endpoint(value, *, allow_path=False):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("endpoint must be an HTTPS URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (parsed.path not in ("", "/") and not allow_path)):
        raise ValueError("endpoint must be HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


def _secret_ref(name, key):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid Kubernetes Secret name")
    if not isinstance(key, str) or not _SECRET_KEY.fullmatch(key):
        raise ValueError("invalid Kubernetes Secret key")
    return {"name": name, "key": key}


def center_values(locale="en"):
    """Return full-stack values with Prometheus Remote Write receiver enabled."""
    values = yaml_module().safe_load(safe_monitoring_values(locale))
    values.setdefault("prometheus", {}).setdefault("prometheusSpec", {})[
        "enableRemoteWriteReceiver"] = True
    return values


def verify_rendered_mode(mode, manifest):
    """Reject Charts that ignore the requested center/Agent topology values."""
    if mode not in ("center", "agent") or not isinstance(manifest, str):
        raise ValueError("invalid topology preflight input")
    yaml = yaml_module()
    try:
        # Helm may include CRD schemas whose enum contains the YAML 1.1 `=`
        # scalar. Use the same compatibility loader as the install post-renderer.
        resources = [item for item in yaml.load_all(manifest, Loader=manifest_loader(yaml))
                     if isinstance(item, dict)]
    except yaml.YAMLError:
        raise ValueError("Helm topology preflight is not valid YAML") from None
    by_kind = {}
    for resource in resources:
        by_kind.setdefault(resource.get("kind"), []).append(resource)
    if mode == "center":
        prometheus = by_kind.get("Prometheus", [])
        if (len(prometheus) != 1 or by_kind.get("PrometheusAgent")
                or not prometheus[0].get("spec", {}).get("enableRemoteWriteReceiver")):
            raise ValueError("selected chart did not render a center Prometheus with Remote Write Receiver")
        return {"mode": mode, "prometheus": len(prometheus)}
    agents = by_kind.get("PrometheusAgent", [])
    if (len(agents) != 1 or by_kind.get("Prometheus") or by_kind.get("Alertmanager")
            or by_kind.get("PrometheusRule") or by_kind.get("AlertmanagerConfig")):
        raise ValueError("selected chart did not render a push-only Agent topology")
    center_components = {"grafana", "alertmanager", "loki"}
    for resource in resources:
        if resource.get("kind") not in {"Deployment", "StatefulSet", "DaemonSet", "Service"}:
            continue
        metadata = resource.get("metadata", {})
        labels = metadata.get("labels", {})
        component_names = {
            str(metadata.get("name", "")).casefold(),
            *(str(value).casefold() for key, value in labels.items()
              if key in {"app", "app.kubernetes.io/name", "app.kubernetes.io/component"}),
        }
        if any(component in value.split("-") for component in center_components
               for value in component_names):
            raise ValueError("selected chart rendered a center-only workload in Agent mode")
    spec = agents[0].get("spec", {})
    if not spec.get("remoteWrite") or not spec.get("externalLabels", {}).get("cluster"):
        raise ValueError("rendered PrometheusAgent is missing Remote Write or cluster identity")
    return {"mode": mode, "prometheusAgent": len(agents)}


def agent_values(cluster_id, remote_write_url, token_secret, ca_secret=None,
                 token_key="token", ca_key="ca.crt"):
    """Return chart values for a scrape-and-forward-only Prometheus Agent."""
    if not isinstance(cluster_id, str) or not _NAME.fullmatch(cluster_id):
        raise ValueError("cluster ID must be a unique DNS label")
    endpoint = _https_endpoint(remote_write_url, allow_path=True)
    if not urlsplit(endpoint).path.rstrip("/").endswith("/api/v1/write"):
        raise ValueError("Remote Write endpoint path must end in /api/v1/write")
    token = _secret_ref(token_secret, token_key)
    tls = {"serverName": urlsplit(endpoint).hostname}
    if ca_secret:
        tls["ca"] = {"secret": _secret_ref(ca_secret, ca_key)}

    safe_defaults = yaml_module().safe_load(safe_monitoring_values())
    # Prometheus Agent has a bounded WAL (roughly two hours) for retrying
    # remote-write failures. It does not support Prometheus TSDB PVC storage.
    # Preserve node textfile collection wiring so central node rules still
    # receive kubelet/containerd samples from the spoke.
    values = {
        # Spoke clusters must expose the same node textfile metrics so the
        # center's kubelet/containerd rules continue to work after migration.
        "prometheus-node-exporter": safe_defaults["prometheus-node-exporter"],
        "defaultRules": {"create": False, "rules": {"kubeProxy": False}},
        "kubeProxy": {"serviceMonitor": {"enabled": False}},
    }
    values.pop("additionalPrometheusRulesMap", None)
    values["prometheus"] = {
        "agentMode": True,
        "prometheusSpec": {
            "externalLabels": {"cluster": cluster_id},
            "remoteWrite": [{
                "url": endpoint,
                "authorization": {"type": "Bearer", "credentials": token},
                "tlsConfig": tls,
                "queueConfig": {
                    "capacity": 10000,
                    "maxShards": 10,
                    "minShards": 1,
                    "maxSamplesPerSend": 2000,
                    "batchSendDeadline": "5s",
                },
            }],
        },
    }
    values["alertmanager"] = {"enabled": False}
    values["grafana"] = {"enabled": False}
    return values


def alloy_remote_write_config(loki_push_url, token_secret_env="ATLAS_LOKI_TOKEN"):
    """Return Alloy HCL for authenticated HTTPS Loki push with normal TLS checks."""
    endpoint = _https_endpoint(loki_push_url, allow_path=True)
    if not isinstance(token_secret_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", token_secret_env):
        raise ValueError("invalid token environment variable name")
    return (
        'loki.write "default" {\n'
        '  endpoint {\n'
        f'    url = "{endpoint}/loki/api/v1/push"\n'
        f'    bearer_token = sys.env("{token_secret_env}")\n'
        '  }\n'
        '}\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("center", "agent"), required=True)
    parser.add_argument("--verify-rendered-manifest")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--cluster-id")
    parser.add_argument("--remote-write-url")
    parser.add_argument("--token-secret")
    parser.add_argument("--ca-secret")
    parser.add_argument("--token-key", default="token")
    parser.add_argument("--ca-key", default="ca.crt")
    args = parser.parse_args()
    if args.verify_rendered_manifest:
        try:
            manifest = Path(args.verify_rendered_manifest).read_text(encoding="utf-8")
            result = verify_rendered_mode(args.mode, manifest)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("topology preflight passed: " + result["mode"])
        return 0
    if args.mode == "center":
        if any((args.cluster_id, args.remote_write_url, args.token_secret, args.ca_secret)):
            parser.error("agent-only parameters cannot be used in center mode")
        values = center_values(args.lang)
    else:
        if not all((args.cluster_id, args.remote_write_url, args.token_secret)):
            parser.error("agent mode requires cluster ID, Remote Write URL, and token Secret")
        values = agent_values(args.cluster_id, args.remote_write_url, args.token_secret,
                              args.ca_secret, args.token_key, args.ca_key)
    sys.stdout.write(yaml_module().safe_dump(values, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    raise SystemExit(main())
