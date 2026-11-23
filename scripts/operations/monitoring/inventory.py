# SPDX-License-Identifier: MIT
"""Read-only Kubernetes inventory; API state is not monitoring coverage."""
from ..models import OperationError
from .publication import OWNER
from . import relay_publication


def _items(client, kind, namespace=None):
    try:
        response = client.get(kind, namespace=namespace)
        items = response["items"]
        if not isinstance(items, list):
            raise ValueError("invalid list")
        return items
    except (OperationError, KeyError, TypeError, ValueError):
        return None


def _email_template_status(client):
    """Inspect native HTML or the script-managed HTML relay without exposing secrets."""
    items = _items(client, "alertmanagerconfigs.monitoring.coreos.com")
    if items is None:
        return {"state": "unverified", "html": "unverified", "text": "unverified", "routes": 0}
    owned = []
    for item in items:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        if (isinstance(metadata, dict) and metadata.get("name") == "atlas-email" and
                isinstance(labels, dict) and labels.get(OWNER) == "monitoring"):
            owned.append(item)
    if not owned:
        return _relay_email_template_status(client, items)

    email_configs = []
    for item in owned:
        spec = item.get("spec")
        receivers = spec.get("receivers") if isinstance(spec, dict) else None
        if not isinstance(receivers, list):
            return {"state": "unverified", "html": "unverified", "text": "unverified",
                    "routes": len(owned)}
        for receiver in receivers:
            if not isinstance(receiver, dict) or receiver.get("name") != "atlas-email":
                continue
            configs = receiver.get("emailConfigs")
            if not isinstance(configs, list):
                return {"state": "unverified", "html": "unverified", "text": "unverified",
                        "routes": len(owned)}
            email_configs.extend(configs)
    if not email_configs or any(not isinstance(value, dict) for value in email_configs):
        return {"state": "unverified", "html": "unverified", "text": "unverified",
                "routes": len(owned)}

    html_values = [value.get("html") for value in email_configs]
    text_values = [value.get("text") for value in email_configs]
    html_status = ("styled" if all(isinstance(value, str) and
                    all(marker in value for marker in ("<table", "background-color:#348bd4", "#f3f4f6"))
                    for value in html_values) else "missing")
    # The managed route intentionally omits text/plain so clients cannot select
    # the unstyled alternative. Older routes with a text body need republishing.
    text_status = "disabled" if all(value in (None, "") for value in text_values) else "enabled"
    state = "present" if html_status == "styled" and text_status == "disabled" else "incomplete"
    return {"state": state, "html": html_status, "text": text_status, "routes": len(owned)}


def _relay_email_template_status(client, alertmanager_configs):
    """Verify relay route, selected CR shape, and content-addressed HTML renderer bundle."""
    missing = {"state": "missing", "html": "missing", "text": "disabled", "routes": 0}
    try:
        config_map = client.get("configmap", relay_publication.CONFIG_NAME, "monitoring", optional=True)
        if config_map is None:
            return missing
        relay_publication._identity(config_map, "data")
        body = relay_publication._route_document(config_map)
        email_routes = [route for route in body["routes"] if route.get("kind") == "email"]
        if not email_routes:
            return missing

        route_names = {route["receiver"] for route in email_routes}
        managed_routes = []
        release_values = set()
        for item in alertmanager_configs:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            spec = item.get("spec", {}) if isinstance(item, dict) else {}
            route = spec.get("route", {}) if isinstance(spec, dict) else {}
            receivers = spec.get("receivers", []) if isinstance(spec, dict) else []
            if (not isinstance(labels, dict) or labels.get(OWNER) != "monitoring" or
                    route.get("receiver") not in route_names or not isinstance(receivers, list)):
                continue
            receiver_name = route["receiver"]
            receiver = next((entry for entry in receivers
                             if isinstance(entry, dict) and entry.get("name") == receiver_name), None)
            webhooks = receiver.get("webhookConfigs", []) if isinstance(receiver, dict) else []
            if any(isinstance(webhook, dict) and
                   webhook.get("url") == "http://127.0.0.1:" + str(relay_publication.PORT) + "/v1/alert"
                   for webhook in webhooks):
                managed_routes.append(item)
                release_values.add(labels.get("release"))
        if not managed_routes or len(release_values) != 1:
            return {"state": "incomplete", "html": "unverified", "text": "disabled",
                    "routes": len(email_routes)}

        release = next(iter(release_values))
        expected_code = relay_publication.code_config_map("monitoring", release)
        code_map = client.get("configmap", expected_code["metadata"]["name"], "monitoring", optional=True)
        code_identity = relay_publication._code_identity(code_map)
        expected_digest = expected_code["metadata"]["annotations"][relay_publication.SPEC_HASH]
        if code_identity is None or code_identity["specHash"] != expected_digest:
            return {"state": "incomplete", "html": "missing", "text": "disabled",
                    "routes": len(email_routes)}
        return {"state": "present", "html": "styled", "text": "disabled", "routes": len(email_routes)}
    except (OperationError, KeyError, TypeError, ValueError):
        return {"state": "unverified", "html": "unverified", "text": "unverified", "routes": 0}


def collect(client):
    identity = client.cluster_identity()
    if not isinstance(identity, str) or not identity:
        raise OperationError("ops.change.kubernetes")
    nodes = _items(client, "node")
    node_results = []
    if nodes is not None:
        for node in nodes:
            try:
                name = node["metadata"]["name"]
                conditions = node.get("status", {}).get("conditions", [])
                ready = [entry.get("status") for entry in conditions
                         if entry.get("type") == "Ready"]
                state = ready[0] if len(ready) == 1 and ready[0] in ("True", "False", "Unknown") else "Unknown"
                if not isinstance(name, str) or not name:
                    raise ValueError("missing name")
                node_results.append({"name": name, "ready": state})
            except (KeyError, TypeError, AttributeError, ValueError):
                nodes = None
                node_results = []
                break
    try:
        namespace = client.get("namespace", "monitoring", optional=True)
        if namespace is not None and namespace.get("metadata", {}).get("name") != "monitoring":
            raise ValueError("unexpected namespace")
        namespace_status = "present" if namespace is not None else "absent"
    except (OperationError, AttributeError, KeyError, TypeError, ValueError):
        namespace_status = "unverified"
    workloads = {}
    if namespace_status == "present":
        for kind in ("deployment", "statefulset", "daemonset"):
            items = _items(client, kind, "monitoring")
            workloads[kind] = None if items is None else len(items)
    return {
        "schemaVersion": 1,
        "clusterIdentity": identity,
        "nodes": {"state": "unverified" if nodes is None else "observed",
                  "items": sorted(node_results, key=lambda item: item["name"])},
        "monitoringNamespace": namespace_status,
        "workloadCounts": workloads,
        "emailTemplate": _email_template_status(client),
        "coverage": "unverified",
    }
