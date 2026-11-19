# SPDX-License-Identifier: MIT
"""Expiry and readiness rules for explicitly registered cert-manager Certificates."""
from .rules import _DNS_LABEL, _dns_name, _message


def certificate_rules(namespace, release, certificates, expiry_days, hold_seconds, max_age_seconds,
                      severity, expiry_summary, expiry_description, readiness_summary,
                      readiness_description, telemetry_summary, telemetry_description):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(certificates, (list, tuple)) or not 1 <= len(certificates) <= 1000 or
            type(expiry_days) is not int or not 1 <= expiry_days <= 365 or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or
            not all(_message(value) for value in (expiry_summary, expiry_description,
                                                   readiness_summary, readiness_description,
                                                   telemetry_summary, telemetry_description))):
        raise ValueError("invalid certificate rule configuration")

    seen, generated = set(), []
    for item in certificates:
        if (not isinstance(item, dict) or set(item) != {"namespace", "name"} or
                not isinstance(item["namespace"], str) or not _DNS_LABEL.fullmatch(item["namespace"]) or
                not _dns_name(item["name"])):
            raise ValueError("invalid Certificate target")
        key = item["namespace"], item["name"]
        if key in seen:
            raise ValueError("duplicate Certificate target")
        seen.add(key)
        # With Prometheus honorLabels=false, cert-manager's namespace label is
        # retained as exported_namespace because the scrape target already has
        # its own namespace label (the cert-manager controller namespace).
        labels = 'exported_namespace="' + key[0] + '",name="' + key[1] + '"'
        expiration = "certmanager_certificate_expiration_timestamp_seconds{" + labels + "}"
        age = "(time() - timestamp(" + expiration + "))"
        fresh = "(" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10)"
        readiness = ('certmanager_certificate_ready_status{condition=~"False|Unknown",' + labels + '}')
        readiness_age = "(time() - timestamp(" + readiness + "))"
        fresh_readiness = "(" + readiness_age + " <= " + str(max_age_seconds) + ") and (" + readiness_age + " >= -10)"
        remaining = "(" + expiration + " - time())"
        threshold = str(expiry_days * 86400)
        generated.extend((
            {"alert": "AtlasCertificateExpiringSoon", "expr": "max by (namespace,name) (" +
             remaining + " and " + remaining + " > 0 and " + remaining + " <= " + threshold + " and " + fresh + ")",
             "for": str(hold_seconds) + "s",
             "labels": {"namespace": key[0], "name": key[1], "risk_id": "R-EDGE-04", "severity": severity},
             "annotations": {"summary": expiry_summary, "description": expiry_description,
                             "observed_value": "{{ printf \"%.0f\" $value }} seconds remaining"}},
            {"alert": "AtlasCertificateTelemetryMissing", "expr": "(absent(" + expiration + " and " + fresh +
             ") and absent(" + readiness + " and " + fresh_readiness + "))",
             "for": str(hold_seconds) + "s",
             "labels": {"namespace": key[0], "name": key[1], "risk_id": "R-EDGE-04", "severity": severity},
             "annotations": {"summary": telemetry_summary, "description": telemetry_description}},
            {"alert": "AtlasCertificateNotReady", "expr":
             "max by (namespace,name) ((" + readiness + " == 1) and " + fresh_readiness + ")",
             "for": str(hold_seconds) + "s",
             "labels": {"namespace": key[0], "name": key[1], "risk_id": "R-EDGE-04", "severity": severity},
             "annotations": {"summary": readiness_summary, "description": readiness_description}},
        ))
    generated.sort(key=lambda rule: (rule["labels"]["namespace"], rule["labels"]["name"], rule["alert"]))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-certificate-expiry", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.certificate.expiry", "rules": generated}]}}
