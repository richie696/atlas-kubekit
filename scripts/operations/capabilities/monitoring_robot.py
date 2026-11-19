# SPDX-License-Identifier: MIT
"""Plan and publish one authenticated Alertmanager robot route."""
import copy

from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import alert_routing, relay_publication as publication


def _route(value):
    try:
        return publication._route(value)
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None


def _manifest(value):
    try:
        return publication.alertmanager_config(value["namespace"], value["release"],
            value["route"]["receiver"], value["severity"], value["authSecret"], value["authKey"],
            value["routePolicy"])
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "managerName", "version", "image",
                "authSecret", "authKey", "route", "severity", "expected"}
    required.add("routePolicy")
    if (not isinstance(value, dict) or set(value) - (required | {"diagnostics"}) or
            not required <= set(value) or not isinstance(value["expected"], dict)):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result.setdefault("diagnostics", None)
    if result["diagnostics"] is not None:
        try:
            from ..monitoring.log_diagnostics import validate_settings
            result["diagnostics"] = validate_settings(result["diagnostics"])
        except (TypeError, ValueError):
            raise OperationError("ops.change.invalid_parameters") from None
    result["configuration"] = normalize(result["configuration"])
    result["route"] = _route(result["route"])
    try:
        policy = result["routePolicy"]
        if not isinstance(policy, dict) or set(policy) != {
                "matchers", "groupBy", "groupWait", "groupInterval", "repeatInterval"}:
            raise ValueError("invalid route policy")
        result["routePolicy"] = alert_routing.route_policy(
            policy["matchers"], policy["groupBy"], policy["groupWait"],
            policy["groupInterval"], policy["repeatInterval"])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    if (not isinstance(result["managerName"], str) or not isinstance(result["version"], str) or
            not isinstance(result["image"], str) or not isinstance(result["authSecret"], str) or
            not isinstance(result["authKey"], str)):
        raise OperationError("ops.change.invalid_parameters")
    try:
        publication.container(result["image"], result["authSecret"], result["authKey"], [result["route"]])
        _manifest(result)
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    if result["severity"] not in ("critical", "warning", "info"):
        raise OperationError("ops.change.invalid_parameters")
    expected = result["expected"]
    if set(expected) != {"manager", "credentials", "configMap", "codeMap", "route"}:
        raise OperationError("ops.change.invalid_parameters")
    if (not isinstance(expected["manager"], dict) or set(expected["manager"]) != {"uid", "specHash"} or
            not all(isinstance(item, str) and 1 <= len(item) <= 128 for item in expected["manager"].values()) or
            not isinstance(expected["credentials"], list) or len(expected["credentials"]) > 3):
        raise OperationError("ops.change.invalid_parameters")
    for identity in expected["credentials"]:
        if (not isinstance(identity, dict) or set(identity) != {"name", "key", "uid", "resourceVersion"} or
                not all(isinstance(item, str) and 1 <= len(item) <= 253 for item in identity.values())):
            raise OperationError("ops.change.invalid_parameters")
    for key in ("configMap", "route"):
        identity = expected[key]
        if identity is None:
            continue
        if (not isinstance(identity, dict) or set(identity) != {"uid", "resourceVersion", "specHash"} or
                not all(isinstance(item, str) and 1 <= len(item) <= 128 for item in identity.values())):
            raise OperationError("ops.change.invalid_parameters")
    identity = expected["codeMap"]
    if identity is not None and (not isinstance(identity, dict) or
            set(identity) != {"name", "uid", "resourceVersion", "specHash"} or
            not all(isinstance(item, str) and 1 <= len(item) <= 128 for item in identity.values())):
        raise OperationError("ops.change.invalid_parameters")
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    if args.monitoring_robot_kind == "email":
        route = {"receiver": args.monitoring_robot_receiver, "kind": "email",
                 "language": args.monitoring_notification_language, "secretName": args.monitoring_robot_secret,
                 "passwordKey": args.monitoring_robot_endpoint_key, "smtpHost": args.monitoring_smtp_host,
                 "smtpPort": args.monitoring_smtp_port, "sender": args.monitoring_email_sender,
                 "recipient": args.monitoring_email_recipient, "username": args.monitoring_email_username}
    else:
        route = {"receiver": args.monitoring_robot_receiver, "kind": args.monitoring_robot_kind,
                 "language": args.monitoring_notification_language, "secretName": args.monitoring_robot_secret,
                 "endpointKey": args.monitoring_robot_endpoint_key,
                 "secretKey": (args.monitoring_robot_signing_key
                               if args.monitoring_robot_signing_key not in (None, "", "-") else None)}
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "managerName": args.monitoring_alertmanager_name,
              "version": args.monitoring_alertmanager_version,
              "image": publication.runtime_image(args.monitoring_relay_image),
              "authSecret": args.monitoring_relay_auth_secret,
              "authKey": args.monitoring_relay_auth_key, "route": route,
              "severity": args.monitoring_severity, "routePolicy": alert_routing.from_arguments(args),
              "diagnostics": None}
    loki_endpoint = getattr(args, "monitoring_loki_endpoint", None)
    if loki_endpoint:
        values["diagnostics"] = {"loki_endpoint": loki_endpoint,
                                  "before_seconds": getattr(args, "monitoring_log_before_seconds", 300),
                                  "after_seconds": getattr(args, "monitoring_log_after_seconds", 60),
                                  "max_lines": getattr(args, "monitoring_log_max_lines", 20),
                                  "max_message_bytes": getattr(args, "monitoring_log_max_message_bytes", 8192)}
        grafana_url = getattr(args, "monitoring_grafana_explore_url", None)
        datasource_uid = getattr(args, "monitoring_loki_datasource_uid", None)
        if grafana_url or datasource_uid:
            values["diagnostics"].update({"grafana_explore_url": grafana_url,
                                          "loki_datasource_uid": datasource_uid,
                                          "grafana_org_id": getattr(args, "monitoring_grafana_org_id", 1)})
    try:
        values["expected"] = publication.capture(environment.client, values)
        publication.preflight(environment.client, values)
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    return parameters(values)


def preview(value):
    values = parameters(value)
    route = values["route"]
    if route["kind"] == "email":
        return (("ops.monitoring.email_diagnostic_preview", (values["managerName"], route["receiver"],
                route["smtpHost"], route["smtpPort"], route["sender"], route["recipient"],
                values["severity"], route["secretName"], route["passwordKey"],
                values["diagnostics"]["loki_endpoint"] if values["diagnostics"] else "—")),
                ("ops.monitoring.route_policy_preview", (
                    ",".join(item["name"] + item["matchType"] + item["value"]
                             for item in values["routePolicy"]["matchers"]),
                    ",".join(values["routePolicy"]["groupBy"]), values["routePolicy"]["groupWait"],
                    values["routePolicy"]["groupInterval"], values["routePolicy"]["repeatInterval"])),
                ("ops.monitoring.email_warning", ()))
    return (("ops.monitoring.robot_preview", (values["managerName"], values["image"], values["namespace"],
             route["kind"], route["receiver"], values["severity"], route["secretName"], route["endpointKey"])),
            ("ops.monitoring.route_policy_preview", (
                ",".join(item["name"] + item["matchType"] + item["value"]
                         for item in values["routePolicy"]["matchers"]),
                ",".join(values["routePolicy"]["groupBy"]), values["routePolicy"]["groupWait"],
                values["routePolicy"]["groupInterval"], values["routePolicy"]["repeatInterval"])),
            ("ops.monitoring.robot_secret_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    current = publication.capture(environment.client, values)
    if current != values["expected"]:
        raise OperationError("ops.change.stale")
    desired = publication.desired(values)
    return {"configurationRevision": revision(config), "target": current,
            "desiredHash": fingerprint(desired)}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    checkpoint("monitoring-robot")
    return publication.publish(environment.client, values)
