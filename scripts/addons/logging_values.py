# SPDX-License-Identifier: MIT
"""Render bounded, persistent Loki and Kubernetes Pod-log Alloy values."""
import argparse
import json
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.addons.sources import yaml_module


def _validate(retention_days, storage_class, storage_size, namespaces):
    if not isinstance(retention_days, int) or not 1 <= retention_days <= 365:
        raise ValueError("retention must be between 1 and 365 days")
    if storage_class is not None and not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", storage_class):
        raise ValueError("invalid StorageClass name")
    if not re.fullmatch(r"[1-9][0-9]*(Mi|Gi|Ti)", storage_size):
        raise ValueError("storage size must look like 30Gi")
    if namespaces != "all":
        values = namespaces.split(",")
        if (not values or any(not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", item)
                              or len(item) > 63 for item in values)
                or len(set(values)) != len(values)):
            raise ValueError("namespaces must be 'all' or a unique comma-separated list of namespace names")
    return namespaces.split(",") if namespaces != "all" else None


def render(retention_days=7, storage_class=None, storage_size="30Gi", namespaces="all",
           remote_loki_url=None, remote_token_secret=None, remote_ca_secret=None,
           remote_token_key="token", remote_ca_key="ca.crt", cluster_id=None):
    """Return chart values; filesystem mode is persistent but single-replica/non-HA."""
    selected_namespaces = _validate(retention_days, storage_class, storage_size, namespaces)
    if remote_loki_url:
        endpoint = urlsplit(remote_loki_url)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or not endpoint.path.endswith("/loki/api/v1/push")):
            raise ValueError("remote Loki endpoint must be an HTTPS /loki/api/v1/push URL")
        if not isinstance(cluster_id, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", cluster_id):
            raise ValueError("a unique cluster ID is required for remote Loki labels")
        if not remote_token_secret or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", remote_token_secret):
            raise ValueError("a valid Kubernetes Secret is required for remote Loki authentication")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", remote_token_key):
            raise ValueError("invalid remote Loki token Secret key")
        if remote_ca_secret and not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", remote_ca_secret):
            raise ValueError("invalid remote Loki CA Secret name")
        if remote_ca_secret and not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", remote_ca_key):
            raise ValueError("invalid remote Loki CA Secret key")
    elif remote_token_secret or remote_ca_secret:
        raise ValueError("remote Loki Secret references require a remote endpoint")
    loki = {
        "deploymentMode": "SingleBinary",
        "loki": {
            "auth_enabled": False,
            "commonConfig": {"replication_factor": 1},
            "schemaConfig": {"configs": [{
                "from": "2024-04-01", "store": "tsdb", "object_store": "filesystem",
                "schema": "v13", "index": {"prefix": "index_", "period": "24h"},
            }]},
            "storage": {
                "type": "filesystem",
                "bucketNames": {"chunks": "chunks", "ruler": "ruler", "admin": "admin"},
                "filesystem": {"chunks_directory": "/var/loki/chunks", "rules_directory": "/var/loki/rules"},
            },
            "limits_config": {"retention_period": f"{retention_days * 24}h"},
            "compactor": {"retention_enabled": True, "delete_request_store": "filesystem"},
        },
        "singleBinary": {
            "replicas": 1,
            "persistence": {"enabled": storage_class is not None, "size": storage_size},
            "resources": {"requests": {"cpu": "100m", "memory": "512Mi"}},
        },
        "backend": {"replicas": 0}, "read": {"replicas": 0}, "write": {"replicas": 0},
        "gateway": {"enabled": False},
        "chunksCache": {"enabled": False}, "resultsCache": {"enabled": False},
    }
    if storage_class is not None:
        loki["singleBinary"]["persistence"]["storageClass"] = storage_class

    hcl = [
        'logging {',
        '  level = "info"',
        '  format = "logfmt"',
        '}',
        'discovery.kubernetes "pods" {',
        '  role = "pod"',
        '}',
        'discovery.relabel "selected_pods" {',
        '  targets = discovery.kubernetes.pods.targets',
    ]
    if selected_namespaces:
        # JSON string escaping is compatible with quoted Alloy/HCL strings.
        # Kubernetes namespace names contain only lowercase letters, digits
        # and hyphens, so no regex escaping is needed; Go RE2 doesn't support
        # non-capturing groups.
        namespace_pattern = "^(" + "|".join(selected_namespaces) + ")$"
        hcl.extend((
            '  rule {',
            '    source_labels = ["__meta_kubernetes_namespace"]',
            f'    regex = {json.dumps(namespace_pattern)}',
            '    action = "keep"',
            '  }',
        ))
    for source, target in (
        ("__meta_kubernetes_namespace", "namespace"),
        ("__meta_kubernetes_pod_name", "pod"),
        ("__meta_kubernetes_pod_container_name", "container"),
        ("__meta_kubernetes_pod_node_name", "node"),
    ):
        hcl.extend((
            '  rule {',
            f'    source_labels = ["{source}"]',
            f'    target_label = "{target}"',
            '  }',
        ))
    if remote_loki_url:
        hcl.extend((
            '  rule {',
            '    target_label = "cluster"',
            f'    replacement = {json.dumps(cluster_id)}',
            '  }',
        ))
    hcl.extend((
        '}',
        'loki.source.kubernetes "pods" {',
        '  targets = discovery.relabel.selected_pods.output',
        '  forward_to = [loki.write.default.receiver]',
        '}',
    ))
    if remote_loki_url:
        hcl.extend((
            'loki.write "default" {',
            '  endpoint {',
            f'    url = {json.dumps(remote_loki_url)}',
            '    bearer_token = sys.env("ATLAS_LOKI_TOKEN")',
        ))
        if remote_ca_secret:
            hcl.extend((
                '    tls_config {',
                '      ca_file = "/etc/atlas-loki-ca/ca.crt"',
                '    }',
            ))
        hcl.extend(('  }', '}'))
    else:
        hcl.extend((
            'loki.write "default" {',
            '  endpoint {',
            '    url = "http://loki.logging.svc.cluster.local:3100/loki/api/v1/push"',
            '  }',
            '}',
        ))
    alloy = {
        # loki.source.kubernetes reads all selected Pods through the API. A
        # DaemonSet would make every Alloy replica tail the same Pods and
        # duplicate every log line, so keep exactly one cluster collector.
        "controller": {"type": "deployment", "replicas": 1},
        "alloy": {
            "enableReporting": False,
            "mounts": {"varlog": False},
            "configMap": {"content": "\n".join(hcl) + "\n"},
        },
        "rbac": {
            "create": True,
            "rules": [
                {"apiGroups": [""], "resources": ["pods"],
                 "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ],
            # The chart defaults also grant nodes, node metrics and /metrics.
            # Pod discovery needs only namespace discovery at cluster scope.
            "clusterRules": [{"apiGroups": [""], "resources": ["namespaces"],
                              "verbs": ["get", "list", "watch"]}],
        },
        "service": {"enabled": False},
    }
    if remote_loki_url:
        alloy["alloy"]["extraEnv"] = [{
            "name": "ATLAS_LOKI_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": remote_token_secret, "key": remote_token_key}},
        }]
        if remote_ca_secret:
            alloy["volumes"] = {"extra": [{
                "name": "atlas-loki-ca", "secret": {
                    "secretName": remote_ca_secret,
                    "items": [{"key": remote_ca_key, "path": "ca.crt"}],
                },
            }]}
            alloy["alloy"]["mounts"]["extra"] = [{
                "name": "atlas-loki-ca", "mountPath": "/etc/atlas-loki-ca",
                "readOnly": True,
            }]
    yaml = yaml_module()
    return (yaml.safe_dump(loki, allow_unicode=True, sort_keys=False),
            yaml.safe_dump(alloy, allow_unicode=True, sort_keys=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retention-days", required=True, type=int)
    parser.add_argument("--storage-class", default="none")
    parser.add_argument("--storage-size", default="30Gi")
    parser.add_argument("--namespaces", default="all")
    parser.add_argument("--remote-loki-url")
    parser.add_argument("--remote-token-secret")
    parser.add_argument("--remote-token-key", default="token")
    parser.add_argument("--remote-ca-secret")
    parser.add_argument("--remote-ca-key", default="ca.crt")
    parser.add_argument("--cluster-id")
    parser.add_argument("--loki-output", required=True)
    parser.add_argument("--alloy-output", required=True)
    args = parser.parse_args()
    try:
        loki, alloy = render(args.retention_days,
                             None if args.storage_class == "none" else args.storage_class,
                             args.storage_size, args.namespaces, args.remote_loki_url,
                             args.remote_token_secret, args.remote_ca_secret,
                             args.remote_token_key, args.remote_ca_key, args.cluster_id)
        with open(args.loki_output, "w", encoding="utf-8") as stream:
            stream.write(loki)
        with open(args.alloy_output, "w", encoding="utf-8") as stream:
            stream.write(alloy)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
