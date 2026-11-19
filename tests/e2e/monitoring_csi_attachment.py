# SPDX-License-Identifier: MIT
"""Exercise a real CSI ControllerPublish failure and recovery in a disposable kind lab."""
import copy
import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import yaml

from scripts.addons.monitoring_values import safe_monitoring_values
from tests.e2e.monitoring_kubernetes import command


NAMESPACE = "monitoring"
CLAIM = "atlas-csi-attach-e2e"
POD = "atlas-csi-attach-e2e"
RULE = "AtlasCSIVolumeAttachmentNotReadyE2E"
STORAGE_CLASS = "atlas-csi-e2e-immediate"


def _get_json(base_url, path, query=None):
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urlencode(query)
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def _query(base_url, expression):
    response = _get_json(base_url, "/api/v1/query", {"query": expression})
    if response.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return response.get("data", {}).get("result", [])


def _wait_until(description, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as error:
            last_error = error
        time.sleep(3)
    suffix = ": " + str(last_error) if last_error else ""
    raise RuntimeError(description + " was not observed before timeout" + suffix)


def _managed_rule(alert_name):
    values = yaml.safe_load(safe_monitoring_values("zh_CN"))
    for configured in values["additionalPrometheusRulesMap"].values():
        for group in configured["groups"]:
            for rule in group["rules"]:
                if rule.get("alert") == alert_name:
                    return copy.deepcopy(rule)
    raise RuntimeError("managed CSI attachment rule is missing from safe defaults")


def _volume_attachment(kubectl, persistent_volume, node):
    objects = json.loads(command(kubectl + ["get", "volumeattachment", "-o", "json"]))["items"]
    matches = [item for item in objects
               if item.get("spec", {}).get("source", {}).get("persistentVolumeName") == persistent_volume
               and item.get("spec", {}).get("nodeName") == node]
    if len(matches) > 1:
        raise RuntimeError("multiple VolumeAttachments match the isolated test PV")
    return matches[0] if matches else None


def run(kubectl, prometheus_url, worker_node):
    """Stop the real CSI service during a pending attach, then verify recovery and cleanup."""
    base_rule = _managed_rule("AtlasCSIVolumeAttachmentNotReady")
    base_rule["alert"] = RULE
    base_rule["for"] = "2s"
    base_rule["labels"]["e2e"] = "csi-attachment"
    test_rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
                 "metadata": {"name": "atlas-csi-attachment-e2e", "namespace": NAMESPACE,
                              "labels": {"release": "stack", "app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
                 "spec": {"groups": [{"name": "atlas.csi.attachment.e2e", "rules": [base_rule]}]}}
    rule_created = False
    driver_stopped = False
    cluster_kubectl = kubectl[:-2] if kubectl[-2:] == ["-n", NAMESPACE] else kubectl
    default_kubectl = cluster_kubectl + ["-n", "default"]
    try:
        live_rules = _get_json(prometheus_url, "/api/v1/rules").get("data", {}).get("groups", [])
        for expected in ("AtlasCSIVolumeAttachmentNotReady", "AtlasCSIVolumeAttachmentTelemetryMissing"):
            loaded = [rule for group in live_rules for rule in group.get("rules", [])
                      if rule.get("name") == expected]
            if len(loaded) != 1 or loaded[0].get("health") != "ok":
                raise RuntimeError("managed CSI rule was not loaded healthy by Prometheus: " + expected)

        first_pv = json.loads(command(kubectl + ["-n", NAMESPACE, "get", "pvc", "atlas-pvc-e2e", "-o", "json"]))[
            "spec"]["volumeName"]
        _wait_until("the initial CSI VolumeAttachment", lambda: _volume_attachment(
            kubectl, first_pv, worker_node))
        _wait_until("the initial CSI volume attachment success", lambda: _attachment_attached(
            kubectl, first_pv, worker_node))

        command(kubectl + ["apply", "-f", "-"], data=test_rule)
        rule_created = True
        _wait_until("the E2E CSI alert rule to load", lambda: any(
            rule.get("name") == RULE and rule.get("health") == "ok"
            for group in _get_json(prometheus_url, "/api/v1/rules").get("data", {}).get("groups", [])
            for rule in group.get("rules", [])))

        command(cluster_kubectl + ["create", "-f", "-"], data={
            "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": STORAGE_CLASS}, "provisioner": "hostpath.csi.k8s.io",
            "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate",
        })
        command(kubectl + ["create", "-f", "-"], data={
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": CLAIM},
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": STORAGE_CLASS,
                     "resources": {"requests": {"storage": "1Gi"}}},
        })
        command(kubectl + ["wait", "--for=jsonpath={.status.phase}=Bound",
                           "pvc/" + CLAIM, "--timeout=120s"], timeout=130)
        second_pv = json.loads(command(kubectl + ["get", "pvc", CLAIM, "-o", "json"]))[
            "spec"]["volumeName"]
        pv = json.loads(command(cluster_kubectl + ["get", "pv", second_pv, "-o", "json"]))
        affinity = pv.get("spec", {}).get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
        affinity_nodes = {expression.get("values", [None])[0]
                          for term in affinity for expression in term.get("matchExpressions", [])
                          if expression.get("key") == "kubernetes.io/hostname"
                          and expression.get("operator") == "In" and expression.get("values")}
        if affinity_nodes and worker_node not in affinity_nodes:
            raise RuntimeError("CSI hostpath PV topology does not target the isolated CSI node")

        # Provision while CSI is healthy. Stopping the controller before
        # creating the consumer leaves Kubernetes' real attach request pending.
        command(default_kubectl + ["scale", "statefulset/csi-hostpathplugin", "--replicas=0"])
        driver_stopped = True
        command(default_kubectl + ["wait", "--for=delete", "pod/csi-hostpathplugin-0", "--timeout=90s"],
                timeout=100)
        command(kubectl + ["-n", NAMESPACE, "create", "-f", "-"], data={
            "apiVersion": "v1", "kind": "Pod", "metadata": {"name": POD},
            "spec": {"nodeSelector": {"kubernetes.io/hostname": worker_node},
                     "containers": [{"name": "app", "image": "python:3.13-slim", "command": ["sleep", "3600"],
                                     "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                     "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": CLAIM}}]},
        })
        pending = _wait_until("the CSI VolumeAttachment while its controller is unavailable", lambda: _pending_attachment(
            kubectl, second_pv, worker_node))
        attachment_name = pending["metadata"]["name"]
        _wait_until("fresh kube-state-metrics VolumeAttachment info", lambda: len(_query(
            prometheus_url, 'kube_volumeattachment_info{volumeattachment="' + attachment_name + '"}')) == 1)
        _wait_until("fresh pending CSI attached metric", lambda: _metric_value(
            prometheus_url, "kube_volumeattachment_status_attached", attachment_name) == 0)
        _wait_until("CSI attachment pending alert firing", lambda: _alert_firing(prometheus_url, attachment_name))

        command(default_kubectl + ["scale", "statefulset/csi-hostpathplugin", "--replicas=1"])
        command(default_kubectl + ["rollout", "status", "statefulset/csi-hostpathplugin", "--timeout=120s"],
                timeout=130)
        driver_stopped = False
        _wait_until("the CSI VolumeAttachment to recover", lambda: _attachment_attached(
            kubectl, second_pv, worker_node))
        command(kubectl + ["-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/" + POD,
                           "--timeout=180s"], timeout=190)
        _wait_until("the CSI attached metric to recover", lambda: _metric_value(
            prometheus_url, "kube_volumeattachment_status_attached", attachment_name) == 1)
        _wait_until("CSI attachment failure alert to resolve", lambda: _alert_resolved(
            prometheus_url, attachment_name))
        print("Real CSI controller outage/recovery passed: pending VolumeAttachment, KSM attached=0/1, "
              "Chinese Prometheus alert firing/resolved, and Pod mount readiness verified.")
    finally:
        if driver_stopped:
            command(default_kubectl + ["scale", "statefulset/csi-hostpathplugin", "--replicas=1"])
            command(default_kubectl + ["rollout", "status", "statefulset/csi-hostpathplugin", "--timeout=120s"],
                    timeout=130)
        command(kubectl + ["-n", NAMESPACE, "delete", "pod", POD, "--ignore-not-found=true",
                           "--wait=true", "--timeout=90s"], timeout=100)
        command(kubectl + ["-n", NAMESPACE, "delete", "pvc", CLAIM, "--ignore-not-found=true",
                           "--wait=true", "--timeout=90s"], timeout=100)
        command(cluster_kubectl + ["delete", "storageclass", STORAGE_CLASS,
                                   "--ignore-not-found=true", "--wait=true"])
        if rule_created:
            command(kubectl + ["-n", NAMESPACE, "delete", "prometheusrule", "atlas-csi-attachment-e2e",
                               "--ignore-not-found=true", "--wait=true"])


def _pending_attachment(kubectl, persistent_volume, node):
    attachment = _volume_attachment(kubectl, persistent_volume, node)
    if not attachment:
        return None
    status = attachment.get("status", {})
    if status.get("attached") is not True:
        return attachment
    return None


def _attachment_attached(kubectl, persistent_volume, node):
    attachment = _volume_attachment(kubectl, persistent_volume, node)
    return bool(attachment and attachment.get("status", {}).get("attached") is True)


def _metric_value(prometheus_url, metric, attachment_name):
    rows = _query(prometheus_url, metric + '{volumeattachment="' + attachment_name + '"}')
    if len(rows) != 1:
        return None
    return float(rows[0]["value"][1])


def _alert_firing(prometheus_url, attachment_name):
    alerts = _get_json(prometheus_url, "/api/v1/alerts").get("data", {}).get("alerts", [])
    alert = next((item for item in alerts if item.get("labels", {}).get("alertname") == RULE
                  and item.get("labels", {}).get("volumeattachment") == attachment_name), None)
    if not alert or alert.get("state") != "firing":
        return False
    annotations = alert.get("annotations", {})
    return (annotations.get("summary") == "CSI 卷未能正常附加" and
            "external-attacher" in annotations.get("description", ""))


def _alert_resolved(prometheus_url, attachment_name):
    alerts = _get_json(prometheus_url, "/api/v1/alerts").get("data", {}).get("alerts", [])
    return not any(item.get("labels", {}).get("alertname") == RULE and
                   item.get("labels", {}).get("volumeattachment") == attachment_name and
                   item.get("state") == "firing" for item in alerts)
