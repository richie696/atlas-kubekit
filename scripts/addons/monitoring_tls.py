# SPDX-License-Identifier: MIT
"""Validate monitoring HTTPS material and render a managed Kubernetes TLS Secret."""
import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.addons.monitoring_public_access import (
    UI_HOSTS,
    validate_kubernetes_name,
    validate_monitoring_domain,
)


MANAGED_BY = "atlas-kubekit.io/managed-by"
TLS_PURPOSE = "atlas-kubekit.io/purpose"
TLS_PURPOSE_VALUE = "monitoring-public-access-tls"
MIN_VALIDITY_SECONDS = 30 * 24 * 60 * 60


def _openssl(*args):
    try:
        result = subprocess.run(["openssl", *args], text=True, capture_output=True,
                                check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("OpenSSL could not validate TLS material") from None
    if result.returncode:
        raise ValueError("TLS certificate or private key is invalid")
    return result.stdout


def validated_tls_material(cert_path, key_path, domain):
    """Read PEM files and require a matching, valid certificate for all UI hosts."""
    cert_file, key_file = Path(cert_path), Path(key_path)
    domain = validate_monitoring_domain(domain)
    if not cert_file.is_file() or not key_file.is_file():
        raise ValueError("TLS certificate or private key file does not exist")
    if not cert_file.stat().st_size or not key_file.stat().st_size:
        raise ValueError("TLS certificate or private key file is empty")

    _openssl("x509", "-in", str(cert_file), "-noout", "-checkend",
             str(MIN_VALIDITY_SECONDS))
    subject_alt_names = _openssl(
        "x509", "-in", str(cert_file), "-noout", "-text")
    dns_names = re.findall(r"(?:^|[,\s])DNS:([^,\s]+)", subject_alt_names)
    for component in UI_HOSTS:
        hostname = f"{component}.{domain}"
        matches = any(
            hostname == name
            or (name.startswith("*.")
                and hostname.endswith(name[1:])
                and hostname.count(".") == name.count("."))
            for name in dns_names
        )
        if not matches:
            raise ValueError(f"TLS certificate is invalid for {hostname}")
    cert_public_key = _openssl("x509", "-in", str(cert_file), "-pubkey", "-noout")
    private_public_key = _openssl("pkey", "-in", str(key_file), "-passin", "pass:", "-pubout")
    if cert_public_key.strip() != private_public_key.strip():
        raise ValueError("TLS certificate and private key do not match")
    return cert_file.read_bytes(), key_file.read_bytes()


def secret_manifest(name, domain, cert_path, key_path):
    name = validate_kubernetes_name(name, "TLS Secret name")
    cert, key = validated_tls_material(cert_path, key_path, domain)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/tls",
        "metadata": {
            "name": name,
            "namespace": "monitoring",
            "labels": {
                MANAGED_BY: "monitoring",
                TLS_PURPOSE: TLS_PURPOSE_VALUE,
            },
        },
        "data": {
            "tls.crt": base64.b64encode(cert).decode("ascii"),
            "tls.key": base64.b64encode(key).decode("ascii"),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-secret", action="store_true")
    parser.add_argument("--name")
    parser.add_argument("--domain")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args(argv)
    if not args.render_secret or not all((args.name, args.domain, args.cert, args.key)):
        parser.error("--render-secret requires --name, --domain, --cert, and --key")
    try:
        print(json.dumps(secret_manifest(args.name, args.domain, args.cert, args.key),
                         separators=(",", ":")))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
