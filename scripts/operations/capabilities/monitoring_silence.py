# SPDX-License-Identifier: MIT
"""Plan, create and expire bounded Alertmanager silences through the Service Proxy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import getpass
import re

from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import silences
from ..adapters.kubernetes import resource_name

ALERTMANAGER = "alertmanagers.monitoring.coreos.com"


def _identity(value):
    try:
        meta = value["metadata"]
        uid, version = meta["uid"], meta["resourceVersion"]
    except (KeyError, TypeError):
        raise OperationError("ops.change.kubernetes") from None
    if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
        raise OperationError("ops.change.kubernetes")
    return {"uid": uid, "resourceVersion": version}


def _base(value, client):
    namespace = resource_name(value["namespace"])
    manager = client.get(ALERTMANAGER, resource_name(value["managerName"]), namespace)
    service = client.get("service", resource_name(value["serviceName"]), namespace)
    service_ports = service.get("spec", {}).get("ports", [])
    selector = service.get("spec", {}).get("selector", {})
    if (not isinstance(service_ports, list) or not any(
            isinstance(item, dict) and item.get("name") == value["servicePort"] for item in service_ports) or
            not isinstance(selector, dict) or selector.get("alertmanager") != value["managerName"] or
            selector.get("app.kubernetes.io/name") != "alertmanager"):
        raise OperationError("ops.monitoring.silence_service")
    return {"cluster": client.cluster_identity(), "manager": {
                **_identity(manager), "specHash": fingerprint(manager.get("spec", {}))},
            "service": _identity(service)}


def _creator(cluster_identity):
    return "Atlas KubeKit cluster/" + cluster_identity


def _current_silence(value, client):
    api = client.alertmanager_request(value["namespace"], value["serviceName"], value["servicePort"],
                                      "GET", "/api/v2/silences")
    for silence in silences.api_silences(api):
        if isinstance(silence, dict) and silence.get("id") == value["silenceId"]:
            return silence
    return None


def _validate_owned(silence, cluster_identity):
    if (not isinstance(silence, dict) or not isinstance(silence.get("id"), str) or
            not re.fullmatch(r"[A-Fa-f0-9-]{8,128}", silence["id"]) or
            not isinstance(silence.get("createdBy"), str) or
            not silence["createdBy"].startswith(_creator(cluster_identity) + "/") or
            not isinstance(silence.get("status"), dict) or silence["status"].get("state") not in
            ("active", "pending")):
        raise OperationError("ops.monitoring.silence_not_owned")
    return silence


def parameters(value):
    required = {"configuration", "action", "namespace", "managerName", "serviceName", "servicePort",
                "matchers", "duration", "reason", "silenceId", "operator", "expected"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = deepcopy(value)
    result["configuration"] = normalize(result["configuration"])
    try:
        for key in ("namespace", "managerName", "serviceName", "servicePort"):
            result[key] = resource_name(result[key])
        if result["action"] not in ("create", "expire"):
            raise ValueError()
        if (not isinstance(result["operator"], str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", result["operator"]) or
                not isinstance(result["reason"], str) or
                (result["action"] == "create" and not 1 <= len(result["reason"]) <= 512) or
                (result["action"] == "expire" and len(result["reason"]) > 512)):
            raise ValueError()
        if result["action"] == "create":
            result["matchers"] = silences.matchers(result["matchers"])
            result["duration"], _ = silences.duration(result["duration"])
            if result["silenceId"] is not None:
                raise ValueError()
        else:
            if (not isinstance(result["silenceId"], str) or
                    not re.fullmatch(r"[A-Fa-f0-9-]{8,128}", result["silenceId"]) or
                    result["matchers"] not in ([], None) or result["duration"] is not None):
                raise ValueError()
            result["matchers"] = []
    except (TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    expected = result["expected"]
    if set(expected) != {"cluster", "manager", "service", "silence"}:
        raise OperationError("ops.change.invalid_parameters")
    if not isinstance(expected["cluster"], str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", expected["cluster"]):
        raise OperationError("ops.change.invalid_parameters")
    for field in ("manager", "service"):
        item = expected[field]
        required_identity = {"uid", "resourceVersion"} | ({"specHash"} if field == "manager" else set())
        if (not isinstance(item, dict) or set(item) != required_identity or
                any(not isinstance(part, str) or not 1 <= len(part) <= 128 for part in item.values())):
            raise OperationError("ops.change.invalid_parameters")
    if result["action"] == "create" and expected["silence"] is not None:
        raise OperationError("ops.change.invalid_parameters")
    if result["action"] == "expire" and not isinstance(expected["silence"], dict):
        raise OperationError("ops.change.invalid_parameters")
    if result["action"] == "expire":
        item = expected["silence"]
        if (set(item) != {"id", "createdBy", "comment", "state", "endsAt"} or
                item["id"] != result["silenceId"] or
                not isinstance(item["createdBy"], str) or not isinstance(item["comment"], str) or
                item["state"] not in ("active", "pending") or not isinstance(item["endsAt"], str)):
            raise OperationError("ops.change.invalid_parameters")
    return result


def from_arguments(args, config, environment):
    values = {"configuration": config, "action": args.monitoring_silence_action,
              "namespace": args.monitoring_namespace, "managerName": args.monitoring_alertmanager_name,
              "serviceName": args.monitoring_alertmanager_service,
              "servicePort": args.monitoring_alertmanager_service_port,
              "matchers": args.monitoring_silence_matcher or [],
              "duration": args.monitoring_silence_duration if args.monitoring_silence_action == "create" else None,
              "reason": args.monitoring_silence_reason or "", "silenceId": args.monitoring_silence_id,
              "operator": getpass.getuser()}
    values["expected"] = {**_base(values, environment.client), "silence": None}
    if values["action"] == "expire":
        silence = _current_silence(values, environment.client)
        silence = _validate_owned(silence, values["expected"]["cluster"])
        values["expected"]["silence"] = {"id": silence["id"], "createdBy": silence["createdBy"],
            "comment": silence.get("comment", ""), "state": silence["status"]["state"],
            "endsAt": silence.get("endsAt")}
    return parameters(values)


def preview(value):
    value = parameters(value)
    if value["action"] == "create":
        matchers = ", ".join(item["name"] + "=" + item["value"] for item in value["matchers"])
        return (("ops.monitoring.silence_create_preview", (matchers, value["duration"], value["reason"],
                 value["operator"])), ("ops.monitoring.silence_warning", ()))
    return (("ops.monitoring.silence_expire_preview", (value["silenceId"], value["operator"])),
            ("ops.monitoring.silence_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    base = _base(values, environment.client)
    if base != {key: values["expected"][key] for key in ("cluster", "manager", "service")}:
        raise OperationError("ops.change.stale")
    desired = {key: values[key] for key in ("action", "matchers", "duration", "reason", "silenceId", "operator")}
    result = {"configurationRevision": revision(config), "target": base, "desired": fingerprint(desired)}
    if values["action"] == "expire":
        silence = _validate_owned(_current_silence(values, environment.client), base["cluster"])
        current = {"id": silence["id"], "createdBy": silence["createdBy"],
                   "comment": silence.get("comment", ""), "state": silence["status"]["state"],
                   "endsAt": silence.get("endsAt")}
        if current != values["expected"]["silence"]:
            raise OperationError("ops.change.stale")
        result["silence"] = current
    return result


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    base = _base(values, environment.client)
    if base != {key: values["expected"][key] for key in ("cluster", "manager", "service")}:
        raise OperationError("ops.change.stale")
    checkpoint("monitoring-silence")
    if values["action"] == "expire":
        silence = _validate_owned(_current_silence(values, environment.client), base["cluster"])
        current = {"id": silence["id"], "createdBy": silence["createdBy"],
                   "comment": silence.get("comment", ""), "state": silence["status"]["state"],
                   "endsAt": silence.get("endsAt")}
        if current != values["expected"]["silence"]:
            raise OperationError("ops.change.stale")
        environment.client.alertmanager_request(values["namespace"], values["serviceName"],
            values["servicePort"], "DELETE", "/api/v2/silence/" + values["silenceId"])
        return {"state": "expired", "silenceId": values["silenceId"]}
    marker = "atlas-kubekit-plan=" + run_id
    api = silences.api_silences(environment.client.alertmanager_request(values["namespace"], values["serviceName"],
        values["servicePort"], "GET", "/api/v2/silences"))
    for existing in api:
        if isinstance(existing, dict) and marker in existing.get("comment", ""):
            owned = _validate_owned(existing, base["cluster"])
            return {"state": "created", "silenceId": owned["id"], "idempotent": True}
    _, seconds = silences.duration(values["duration"])
    start = datetime.now(timezone.utc)
    ends = start + timedelta(seconds=seconds)
    payload = {"matchers": values["matchers"], "startsAt": start.isoformat(), "endsAt": ends.isoformat(),
               "createdBy": _creator(base["cluster"]) + "/" + values["operator"],
               "comment": marker + " | " + values["reason"]}
    response = environment.client.alertmanager_request(values["namespace"], values["serviceName"],
        values["servicePort"], "POST", "/api/v2/silences", payload)
    silence_id = response.get("silenceID") if isinstance(response, dict) else None
    if not isinstance(silence_id, str) or not re.fullmatch(r"[A-Fa-f0-9-]{8,128}", silence_id):
        raise OperationError("ops.monitoring.silence_api")
    return {"state": "created", "silenceId": silence_id, "idempotent": False}
