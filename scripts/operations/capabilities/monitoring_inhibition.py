# SPDX-License-Identifier: MIT
"""Plan and publish one explicitly owned Alertmanager inhibition policy."""
from copy import deepcopy
import re

from ..configuration import normalize as normalize_configuration, revision
from ..models import OperationError, fingerprint
from ..monitoring import inhibition, publication
from ..adapters.kubernetes import resource_name

ALERTMANAGER = "alertmanagers.monitoring.coreos.com"
CONFIG = "alertmanagerconfigs.monitoring.coreos.com"


def _desired(value):
    return inhibition.manifest(value["namespace"], value["release"], value["policy"])


def parameters(value):
    required = {"configuration", "namespace", "release", "managerName", "version", "policy", "expected"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = deepcopy(value)
    result["configuration"] = normalize_configuration(result["configuration"])
    try:
        for field in ("namespace", "release", "managerName"):
            result[field] = resource_name(result[field])
        if not isinstance(result["version"], str) or not re.fullmatch(
                r"(?:v)?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                result["version"]):
            raise ValueError()
        policy = result["policy"]
        if not isinstance(policy, dict) or set(policy) != {"enabled", "sourceSeverity", "targetSeverity", "equal"}:
            raise ValueError()
        result["policy"] = inhibition.normalize(policy["enabled"], policy["sourceSeverity"],
                                                  policy["targetSeverity"], policy["equal"])
        _desired(result)
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    expected = result["expected"]
    if set(expected) != {"manager", "config"}:
        raise OperationError("ops.change.invalid_parameters")
    if not isinstance(expected["manager"], dict) or set(expected["manager"]) != {"uid", "specHash"}:
        raise OperationError("ops.change.invalid_parameters")
    if any(not isinstance(item, str) or not item for item in expected["manager"].values()):
        raise OperationError("ops.change.invalid_parameters")
    if expected["config"] is not None and (
            not isinstance(expected["config"], dict) or
            set(expected["config"]) != {"uid", "resourceVersion", "specHash"} or
            any(not isinstance(item, str) or not item for item in expected["config"].values())):
        raise OperationError("ops.change.invalid_parameters")
    return result


def _capture(client, value):
    desired = _desired(value)
    namespace = desired["metadata"]["namespace"]
    manager = client.get(ALERTMANAGER, resource_name(value["managerName"]), namespace)
    spec = manager.get("spec", {})
    labels = desired["metadata"]["labels"]
    selector = spec.get("alertmanagerConfigSelector") if isinstance(spec, dict) else None
    namespaces = spec.get("alertmanagerConfigNamespaceSelector") if isinstance(spec, dict) else None
    strategy = spec.get("alertmanagerConfigMatcherStrategy") or {}
    if (not isinstance(selector, dict) or set(selector) - {"matchLabels"} or
            not isinstance(selector.get("matchLabels", {}), dict) or
            not selector.get("matchLabels") or
            any(item != labels.get(key) for key, item in selector["matchLabels"].items()) or
            namespaces not in (None, {}) or not isinstance(strategy, dict) or
            strategy.get("type", "OnNamespace") not in
            ("None", "OnNamespaceExceptForAlertmanagerNamespace") or
            not isinstance(spec.get("version"), str) or spec["version"].lstrip("v") != value["version"]):
        raise OperationError("ops.monitoring.email_incompatible")
    manager_meta = publication._metadata(manager)
    existing = client.get(CONFIG, desired["metadata"]["name"], namespace, optional=True)
    if existing is not None and any(existing.get("metadata", {}).get("labels", {}).get(key) != item
                                    for key, item in labels.items()):
        raise OperationError("ops.change.foreign_resource")
    return {"manager": {"uid": manager_meta["uid"], "specHash": fingerprint(spec)},
            "config": publication._identity(existing)}


def from_arguments(args, config, environment):
    raw = {"configuration": config, "namespace": args.monitoring_namespace,
           "release": args.monitoring_release, "managerName": args.monitoring_alertmanager_name,
           "version": args.monitoring_alertmanager_version,
           "policy": inhibition.normalize(args.monitoring_inhibition_enabled,
               args.monitoring_inhibition_source_severity, args.monitoring_inhibition_target_severity,
               args.monitoring_inhibition_equal_label)}
    raw["expected"] = _capture(environment.client, raw)
    return parameters(raw)


def preview(value):
    value = parameters(value)
    if value["policy"]["enabled"]:
        detail = (value["policy"]["sourceSeverity"], value["policy"]["targetSeverity"],
                  ",".join(value["policy"]["equal"]))
    else:
        detail = ("disabled", "disabled", ",".join(value["policy"]["equal"]))
    return (("ops.monitoring.inhibition_preview", (value["namespace"], value["managerName"], *detail)),
            ("ops.monitoring.inhibition_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    current = _capture(environment.client, values)
    if current != values["expected"]:
        raise OperationError("ops.change.stale")
    desired = _desired(values)
    return {"configurationRevision": revision(config), "target": current,
            "desiredSpecHash": fingerprint(desired["spec"])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    current = _capture(environment.client, values)
    if current != values["expected"]:
        raise OperationError("ops.change.stale")
    checkpoint("monitoring-inhibition")
    desired = _desired(values)
    environment.client.call(["apply", "--dry-run=server", "-f", "-"], value=desired)
    existing = environment.client.get(CONFIG, desired["metadata"]["name"], values["namespace"], optional=True)
    resource = deepcopy(desired if existing is None else existing)
    resource["spec"] = deepcopy(desired["spec"])
    resource.setdefault("metadata", {}).setdefault("labels", {}).update(desired["metadata"]["labels"])
    resource.setdefault("metadata", {}).setdefault("annotations", {})[publication.SPEC_HASH] = fingerprint(resource["spec"])
    saved = environment.client.create(resource) if existing is None else environment.client.replace(resource)
    if saved is None:
        raise OperationError("ops.change.stale")
    verified = _capture(environment.client, values)
    if (verified["manager"] != values["expected"]["manager"] or verified["config"] is None or
            verified["config"]["uid"] != saved["metadata"]["uid"] or
            verified["config"]["specHash"] != fingerprint(desired["spec"])):
        raise OperationError("ops.change.stale")
    return {"state": "resource-written", "config": verified["config"], "activation": "unverified"}
