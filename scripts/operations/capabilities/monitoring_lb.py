# SPDX-License-Identifier: MIT
"""Coordinate LB scrape resources and alert rules as one resumable change."""
import copy

from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import lb_collection, lb_rules, publication
from . import monitoring_lb_collection as collection
from . import monitoring_lb_rules as rules
from . import monitoring_lb_vip as vip_metrics
from . import lb_stats


def _configured(configuration, cidr):
    result = copy.deepcopy(normalize(configuration))
    result["cluster"]["statsAllowedCIDR"] = cidr
    return result


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"access", "collection", "rules", "vipMetrics"}:
        raise OperationError("ops.change.invalid_parameters")
    access = lb_stats.parameters(value["access"])
    scrape = collection.parameters(value["collection"])
    alerting = rules.parameters(value["rules"])
    vip = vip_metrics.parameters(value["vipMetrics"])
    shared = ("configuration", "namespace", "release", "prometheusName", "nodes")
    desired = _configured(access["configuration"], access["cidr"])
    if (scrape["configuration"] != desired or alerting["configuration"] != desired
            or vip["configuration"] != normalize(access["configuration"])
            or any(scrape[key] != alerting[key] for key in shared)):
        raise OperationError("ops.change.invalid_parameters")
    return {"access": access, "collection": scrape, "rules": alerting, "vipMetrics": vip}


def from_arguments(args, configuration, environment):
    config = normalize(configuration)
    cidr = getattr(args, "stats_cidr", None) or config["cluster"]["statsAllowedCIDR"]
    access = lb_stats.parameters({"configuration": config, "cidr": cidr})
    desired = _configured(config, access["cidr"])
    scrape = collection.from_arguments(args, desired, environment)
    alerting = rules.from_arguments(args, desired, environment)
    vip = vip_metrics.from_arguments(args, config, environment)
    return parameters({"access": access, "collection": scrape, "rules": alerting, "vipMetrics": vip})


def preview(value):
    values = parameters(value)
    access_cidr = values["access"]["cidr"]
    return (
        ("ops.monitoring.lb_facade_preview", (
            len(values["collection"]["nodes"]), values["collection"]["namespace"],
            values["collection"]["prometheusName"],
            access_cidr,
        )),
        ("ops.preview.stats", (access_cidr,)),
        *vip_metrics.preview(values["vipMetrics"]),
        *collection.preview(values["collection"]),
        *rules.preview(values["rules"]),
        ("ops.monitoring.lb_facade_resume", ()),
    )


def observe(value, environment):
    values = parameters(value)
    access = lb_stats.observe(values["access"], environment)
    config = normalize(values["collection"]["configuration"])
    return {
        "configurationRevision": revision(config),
        "access": access,
        "vipMetrics": vip_metrics.observe(values["vipMetrics"], environment),
        "scrape": collection.observe(values["collection"], environment),
        "rules": rules.observe(values["rules"], environment),
    }


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    checkpoint("monitoring-lb-access-start")
    access_result = lb_stats.apply(values["access"], environment, checkpoint, run_id)
    checkpoint("monitoring-lb-access-complete")
    checkpoint("monitoring-lb-vip-exporter-start")
    vip_result = vip_metrics.apply(values["vipMetrics"], environment, checkpoint, run_id)
    checkpoint("monitoring-lb-vip-exporter-complete")
    checkpoint("monitoring-lb-scrape-start")
    scrape_result = collection.apply(values["collection"], environment, checkpoint, run_id)
    checkpoint("monitoring-lb-scrape-complete")
    rule_result = rules.apply(values["rules"], environment, checkpoint, run_id)
    checkpoint("monitoring-lb-rules-complete")
    return {"state": "resources-written", "access": access_result, "vipMetrics": vip_result,
            "scrape": scrape_result, "rules": rule_result,
            "verification": "Prometheus target discovery and rule evaluation remain independent checks"}


def continue_parameters(value, environment):
    """Refresh expectations only when each resource is unchanged or already desired."""
    values = parameters(value)
    scrape = values["collection"]
    alerting = values["rules"]
    service, endpoints, monitor = collection._manifests(scrape)
    current_scrape = lb_collection.capture(
        environment.client, service, endpoints, monitor, scrape["prometheusName"]
    )
    old_scrape = scrape["expected"]
    if current_scrape["prometheus"] != old_scrape["prometheus"]:
        raise OperationError("ops.change.stale")
    for manifest, field, key in (
        (service, "spec", "service"),
        (endpoints, "subsets", "endpoints"),
        (monitor, "spec", "servicemonitor"),
    ):
        actual = current_scrape["resources"][key]
        prior = old_scrape["resources"][key]
        wanted_hash = fingerprint(manifest[field])
        if actual != prior and not (actual and actual.get("specHash") == wanted_hash):
            raise OperationError("ops.change.stale")

    rule_manifest = rules._manifest(alerting)
    current_rule = publication.capture(
        environment.client, rule_manifest, alerting["prometheusName"]
    )
    old_rule = alerting["expected"]
    if current_rule["prometheus"] != old_rule["prometheus"]:
        raise OperationError("ops.change.stale")
    actual_rule = current_rule["rule"]
    prior_rule = old_rule["rule"]
    wanted_rule_hash = fingerprint(rule_manifest["spec"])
    if actual_rule != prior_rule and not (actual_rule and actual_rule.get("specHash") == wanted_rule_hash):
        raise OperationError("ops.change.stale")

    refreshed = copy.deepcopy(values)
    refreshed["vipMetrics"]["expected"] = vip_metrics._snapshot(
        refreshed["vipMetrics"]["configuration"], environment
    )
    refreshed["collection"]["expected"] = current_scrape
    refreshed["rules"]["expected"] = current_rule
    return parameters(refreshed)
