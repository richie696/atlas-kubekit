# SPDX-License-Identifier: MIT
"""Safe lifecycle helpers for Secrets created by guided notification setup."""
import re

from ..models import OperationError

OWNER = "monitoring-notification-setup"
OWNER_LABEL = "atlas-kubekit.io/owner"
COMPONENT_LABEL = "atlas-kubekit.io/component"
RECEIVER_LABEL = "atlas-kubekit.io/receiver"
ALERT_CONFIG = "alertmanagerconfigs.monitoring.coreos.com"
ALERTMANAGER = "alertmanagers.monitoring.coreos.com"
MONITORING_REFERENCES = frozenset((
    ALERT_CONFIG, ALERTMANAGER, "prometheuses.monitoring.coreos.com",
    "prometheusagents.monitoring.coreos.com", "thanosrulers.monitoring.coreos.com",
    "servicemonitors.monitoring.coreos.com", "podmonitors.monitoring.coreos.com",
    "probes.monitoring.coreos.com", "scrapeconfigs.monitoring.coreos.com",
))


def labels(component, receiver=None):
    if component not in ("route", "relay-auth"):
        raise ValueError("unsupported notification Secret component")
    result = {OWNER_LABEL: OWNER, COMPONENT_LABEL: component}
    if component == "route":
        if not isinstance(receiver, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", receiver):
            raise ValueError("invalid notification receiver")
        result[RECEIVER_LABEL] = receiver
    elif receiver is not None:
        raise ValueError("relay authorization Secret cannot have a receiver")
    return result


def _collect_secret_names(value, names):
    if isinstance(value, dict):
        # The Operator embeds SecretKeySelector under different field names.
        reference_name = value.get("name")
        if isinstance(reference_name, str) and (
                isinstance(value.get("key"), str) or
                (value.get("apiVersion") == "v1" and value.get("kind") == "Secret")):
            names.add(reference_name)
        for key, item in value.items():
            if key in ("configMap", "configMapKeyRef", "configMapRef"):
                continue
            if key in ("secretName", "configSecret") and isinstance(item, str):
                names.add(item)
            elif key in ("secretKeyRef", "secretRef", "authPassword", "credentials", "secret") or key.endswith(
                    ("Secret", "SecretRef")):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.add(item["name"])
            elif key in ("imagePullSecrets", "secrets") and isinstance(item, list):
                for entry in item:
                    name = entry.get("name") if isinstance(entry, dict) else entry
                    if isinstance(name, str):
                        names.add(name)
            _collect_secret_names(item, names)
    elif isinstance(value, list):
        for item in value:
            _collect_secret_names(item, names)


def referenced_names(client, namespace):
    """Collect references from Kubernetes and installed Prometheus Operator resource types."""
    discovered = set(client.call(
        ["api-resources", "--api-group=monitoring.coreos.com", "--namespaced=true",
         "--verbs=list", "-o", "name"], json_output=False
    ).split())
    if ALERT_CONFIG not in discovered:
        raise OperationError("ops.change.kubernetes")
    names = set()
    kinds = ("pod", "deployment", "daemonset", "replicaset", "statefulset", "job", "cronjob",
             "serviceaccount", "ingress", "persistentvolumeclaim",
             *sorted(discovered & MONITORING_REFERENCES), "persistentvolume")
    for kind in kinds:
        items = client.get(kind, namespace=None if kind == "persistentvolume" else namespace)["items"]
        if not isinstance(items, list):
            raise OperationError("ops.change.kubernetes")
        _collect_secret_names(items, names)
        if kind == ALERTMANAGER:
            for alertmanager in items:
                if not alertmanager["spec"].get("configSecret"):
                    names.add("alertmanager-" + alertmanager["metadata"]["name"])
    return names


def replacement_candidates(client, namespace, receiver, previous_names):
    """Capture identities from the selected receiver's deployed configuration before applying its replacement."""
    labels("route", receiver)
    if not previous_names:
        return ()
    return tuple(identity for identity in client.notification_secret_identities(namespace)
                 if identity["owner"] == OWNER and identity["component"] == "route"
                 and identity["receiver"] == receiver and identity["name"] in previous_names)


def cleanup_unreferenced(client, namespace, candidates):
    """Remove only previously captured credentials no longer referenced by resources."""
    removed, retained = [], []
    for identity in candidates:
        name = identity["name"]
        if name in referenced_names(client, namespace):
            retained.append(name)
            continue
        client.delete_notification_secret(namespace, identity)
        removed.append(name)
    return {"removed": removed, "retained": retained}
