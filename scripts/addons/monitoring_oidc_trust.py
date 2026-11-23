# SPDX-License-Identifier: MIT
"""Build Envoy Gateway resources for OIDC providers using a private CA."""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


OWNER = "atlas-kubekit.io/managed-by"
PURPOSE = "atlas-kubekit.io/purpose"
OIDC_BACKEND = "atlas-monitoring-oidc-provider"
OIDC_CA_CONFIGMAP = "atlas-monitoring-oidc-ca"
OIDC_TLS_POLICY = "atlas-monitoring-oidc-backend-tls"
_PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?\s+-----END CERTIFICATE-----"
)


def resources(issuer, ca_bundle, namespace="monitoring"):
    """Render a CA ConfigMap, Envoy Backend, and BackendTLSPolicy for OIDC."""
    if not isinstance(issuer, str):
        raise ValueError("invalid OIDC issuer")
    parsed = urlsplit(issuer)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OIDC issuer must be an HTTPS URL without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("OIDC provider must use HTTPS port 443")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError("invalid Kubernetes namespace")

    path = Path(ca_bundle)
    try:
        pem = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise ValueError("OIDC CA bundle is not a readable PEM file") from None
    blocks = _PEM_CERTIFICATE.findall(pem)
    if not blocks or "".join(blocks).replace("\r", "").replace("\n", "").replace(" ", "") != \
            pem.replace("\r", "").replace("\n", "").replace(" ", ""):
        raise ValueError("OIDC CA bundle must contain only PEM certificates")

    hostname = parsed.hostname.rstrip(".").lower()
    labels = {OWNER: "monitoring", PURPOSE: "oidc-provider-ca"}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": OIDC_CA_CONFIGMAP, "namespace": namespace, "labels": labels},
         "data": {"ca.crt": pem}},
        {"apiVersion": "gateway.envoyproxy.io/v1alpha1", "kind": "Backend",
         "metadata": {"name": OIDC_BACKEND, "namespace": namespace, "labels": labels},
         "spec": {"endpoints": [{"fqdn": {"hostname": hostname, "port": 443}}]}},
        {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "BackendTLSPolicy",
         "metadata": {"name": OIDC_TLS_POLICY, "namespace": namespace, "labels": labels},
         "spec": {"targetRefs": [{"group": "gateway.envoyproxy.io", "kind": "Backend",
                                   "name": OIDC_BACKEND}],
                  "validation": {"hostname": hostname,
                                 "caCertificateRefs": [{"name": OIDC_CA_CONFIGMAP,
                                                        "group": "", "kind": "ConfigMap"}]}}},
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--issuer")
    parser.add_argument("--ca-bundle")
    parser.add_argument("--namespace", default="monitoring")
    args = parser.parse_args(argv)
    if not args.render or not args.issuer or not args.ca_bundle:
        parser.error("--render requires --issuer and --ca-bundle")
    try:
        sys.stdout.write(json.dumps(resources(args.issuer, args.ca_bundle, args.namespace),
                                   ensure_ascii=False, separators=(",", ":")))
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
