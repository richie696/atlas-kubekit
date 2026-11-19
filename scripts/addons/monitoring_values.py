# SPDX-License-Identifier: MIT
"""Render secure defaults for the kube-prometheus-stack add-on."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lib.i18n import translate
from scripts.addons.sources import yaml_module

SYSTEMD_MOUNT = "/host-textfile"


def safe_monitoring_values(locale="en"):
    """Render monitoring defaults safe for common single-replica installs."""
    alertmanager_target = ('up{job="kube-prometheus-stack-alertmanager",'
                           'endpoint="http-web",namespace="monitoring"}')
    rule = {
        "alert": "AtlasAlertmanagerUnavailableOrTelemetryMissing",
        "expr": f"(absent({alertmanager_target}) or sum({alertmanager_target}) < 1)",
        "for": "1m",
        "labels": {"severity": "critical", "risk_id": "R-OBS-02"},
        "annotations": {
            "summary": translate(locale, "phase03.monitoring.alertmanager_unavailable_summary"),
            "description": translate(locale, "phase03.monitoring.alertmanager_unavailable_description"),
        },
    }
    systemd_rules = []
    file_age = f'time() - node_textfile_mtime_seconds{{file="{SYSTEMD_MOUNT}/atlas_systemd_units.prom"}}'
    fresh_file = f"(({file_age} >= -10) and ({file_age} < 120))"
    for unit in ("kubelet", "containerd"):
        unit_name = "Kubelet" if unit == "kubelet" else "Containerd"
        probe = f'atlas_systemd_unit_probe_success{{unit="{unit}"}}'
        active = f'atlas_systemd_unit_active{{unit="{unit}"}}'
        scrape_fresh = f"(time() - timestamp({probe}) < 120)"
        observed = f"{scrape_fresh} and on(job,instance) {fresh_file}"
        systemd_rules.extend((
            {
                "alert": f"Atlas{unit_name}ServiceUnavailable",
                "expr": (f"((({active} == 0) or ({probe} == 0)) and {observed}) "
                         "* on(job,instance) group_left(nodename) node_uname_info"),
                "for": "2m",
                "labels": {"severity": "critical", "risk_id": "R-NODE-06"},
                "annotations": {
                    "summary": translate(locale, f"phase03.monitoring.{unit}_unavailable_summary"),
                    "description": translate(locale, f"phase03.monitoring.{unit}_unavailable_description"),
                },
            },
            {
                "alert": f"Atlas{unit_name}SystemdTelemetryMissing",
                "expr": (
                    'node_uname_info{nodename!=""} unless on(job,instance) '
                    f'(({probe} == 1) and {active} and {observed})'
                ),
                "for": "2m",
                "labels": {"severity": "critical", "risk_id": "R-NODE-06"},
                "annotations": {
                    "summary": translate(locale, f"phase03.monitoring.{unit}_telemetry_missing_summary"),
                    "description": translate(locale, f"phase03.monitoring.{unit}_telemetry_missing_description"),
                },
            },
        ))
    attachment_info = "kube_volumeattachment_info"
    attachment_status = "kube_volumeattachment_status_attached"

    def fresh(metric):
        age = "time() - timestamp(" + metric + ")"
        return "(" + metric + " and (" + age + " <= 120) and (" + age + " >= -10))"

    fresh_info = fresh(attachment_info)
    fresh_status = fresh(attachment_status)
    csi_attachment_rules = [
        {
            "alert": "AtlasCSIVolumeAttachmentNotReady",
            "expr": ("max by (attacher,node,volumeattachment) ((" + fresh_status +
                     " == 0) * on(volumeattachment) group_left(attacher,node) " + fresh_info + ")"),
            "for": "5m",
            "labels": {"severity": "warning", "risk_id": "R-STOR-02"},
            "annotations": {
                "summary": translate(locale, "phase03.monitoring.csi_attachment_summary"),
                "description": translate(locale, "phase03.monitoring.csi_attachment_description"),
            },
        },
        {
            "alert": "AtlasCSIVolumeAttachmentTelemetryMissing",
            "expr": "max by (attacher,node,volumeattachment) (" + fresh_info +
                    " unless on(volumeattachment) " + fresh_status + ")",
            "for": "5m",
            "labels": {"severity": "warning", "risk_id": "R-STOR-02"},
            "annotations": {
                "summary": translate(locale, "phase03.monitoring.csi_attachment_telemetry_summary"),
                "description": translate(locale, "phase03.monitoring.csi_attachment_telemetry_description"),
            },
        },
    ]
    values = {
        "grafana": {"deploymentStrategy": {"type": "Recreate"}},
        "defaultRules": {"rules": {"kubeProxy": False}},
        "alertmanager": {"config": {"inhibit_rules": [
            {"source_matchers": ["severity = critical"],
             "target_matchers": ["severity =~ warning|info"],
             "equal": ["cluster", "namespace", "alertname"]},
            {"source_matchers": ["severity = warning"],
             "target_matchers": ["severity = info"],
             "equal": ["cluster", "namespace", "alertname"]},
            {"source_matchers": ["alertname = InfoInhibitor"],
             "target_matchers": ["severity = info"],
             "equal": ["cluster", "namespace"]},
            {"target_matchers": ["alertname = InfoInhibitor"]},
        ]}},
        "kubeProxy": {"serviceMonitor": {"enabled": False}},
        "additionalPrometheusRulesMap": {
            "atlas-self-monitoring": {"groups": [{"name": "atlas.monitoring.self", "rules": [rule]}]},
            "atlas-node-systemd": {"groups": [{"name": "atlas.node.systemd", "rules": systemd_rules}]},
            "atlas-csi-attachments": {"groups": [{"name": "atlas.storage.csi-attachments", "rules": csi_attachment_rules}]},
        },
        "prometheus-node-exporter": {
            "extraArgs": ["--collector.textfile.directory=" + SYSTEMD_MOUNT],
            "extraHostVolumeMounts": [{
                "name": "atlas-systemd-textfile",
                "hostPath": str(Path("/var/lib/atlas-kubekit/node-exporter-textfile")),
                "mountPath": SYSTEMD_MOUNT,
                "readOnly": True,
                "type": "DirectoryOrCreate",
            }],
        },
    }
    return yaml_module().safe_dump(values, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--safe-defaults", action="store_true")
    parser.add_argument("--lang", default="en")
    args = parser.parse_args()
    if not args.safe_defaults:
        parser.error("--safe-defaults is required")
    sys.stdout.write(safe_monitoring_values(args.lang))
