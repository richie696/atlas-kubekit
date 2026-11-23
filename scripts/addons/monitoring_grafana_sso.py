# SPDX-License-Identifier: MIT
"""Configure Grafana to verify the ID token forwarded by the monitoring gateway."""
import argparse
import json
import subprocess

from monitoring_public_access import verify_oidc_issuer, validate_oidc_client_id

DEPLOYMENT = "kube-prometheus-stack-grafana"
VOLUME = "atlas-grafana-issuer-ca"


def configure(issuer, client_id, ca):
    metadata = verify_oidc_issuer(issuer, ca_bundle=ca)
    settings = {
        "enabled": "true",
        "header_name": "X-Atlas-ID-Token",
        "username_attribute_path": "join('', ['oidc-', sub])",
        "expect_claims": json.dumps({"iss": issuer, "aud": validate_oidc_client_id(client_id)}),
        "jwk_set_url": metadata["jwks_uri"],
        "auto_sign_up": "true",
        "role_attribute_path": "'Viewer'",
        "allow_assign_grafana_admin": "false",
        "tls_skip_verify_insecure": "false",
        "url_login": "false",
        "tls_client_ca": "/etc/atlas-issuer/ca.crt" if ca else "",
    }
    container = {"name": "grafana", "env": [
        {"name": "GF_AUTH_JWT_" + key.upper(), "value": value}
        for key, value in settings.items()]}
    # Grafana OSS gates Explore separately from the permission to query a data source.
    container["env"].append({"name": "GF_USERS_VIEWERS_CAN_EDIT", "value": "true"})
    spec = {"containers": [container]}
    if ca:
        spec["volumes"] = [{"name": VOLUME, "configMap": {"name": "atlas-monitoring-oidc-ca"}}]
        container["volumeMounts"] = [{"name": VOLUME, "mountPath": "/etc/atlas-issuer", "readOnly": True}]
    else:
        spec["volumes"] = [{"name": VOLUME, "$patch": "delete"}]
        container["volumeMounts"] = [{"mountPath": "/etc/atlas-issuer", "$patch": "delete"}]
    base = ["kubectl", "-n", "monitoring"]
    subprocess.run(base + ["patch", "deploy", DEPLOYMENT, "--type=strategic", "-p",
                           json.dumps({"spec": {"template": {"spec": spec}}})], check=True)
    subprocess.run(base + ["rollout", "status", "deployment/" + DEPLOYMENT, "--timeout=5m"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--ca")
    args = parser.parse_args()
    configure(args.issuer, args.client_id, args.ca)
