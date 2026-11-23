# SPDX-License-Identifier: MIT
"""Correlate an interactive OIDC login with Envoy data-plane evidence."""
import argparse
import base64
import json
import secrets
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

SELECTOR = ("gateway.envoyproxy.io/owning-gateway-name=atlas-monitoring-public,"
            "gateway.envoyproxy.io/owning-gateway-namespace=monitoring")


def callback_target(query):
    try:
        state, = parse_qs(query)["state"]
        raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
        return json.loads(raw)["url"]
    except (KeyError, TypeError, ValueError):
        return None


def inspect(lines, target):
    """Only the callback and upstream response for this one random URL count."""
    redirect = upstream = False
    url = urlsplit(target)
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get(":authority") != url.netloc:
            continue
        path = row.get("x-envoy-origin-path") or ""
        status = row.get("response_code")
        if path == url.path + "?" + url.query and row.get("upstream_host"):
            if status in (401, 403):
                return "grafana-user-access-denied"
            if status == 200:
                upstream = True
        if path.partition("?")[0] != "/oauth2/callback":
            continue
        if callback_target(path.partition("?")[2]) != target:
            continue
        if status == 302:
            redirect = True
        elif status and status >= 400:
            detail = row.get("response_code_details") or ""
            if "签名私钥不可用" in detail:
                return "issuer-signing-key-unavailable"
            return "oidc-callback-failed"
    return "passed" if redirect and upstream else "pending"


def verify(domain, timeout=300):
    target = f"https://grafana.{domain}/api/user?atlas_check={secrets.token_hex(16)}"
    print(target, flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "--request-timeout=10s", "-n", "envoy-gateway-system", "logs",
             "-l", SELECTOR, "-c", "envoy", "--since=6m", "--tail=3000"],
            capture_output=True, text=True, timeout=20)
        if result.returncode:
            return "gateway-log-unavailable"
        state = inspect(result.stdout.splitlines(), target)
        if state != "pending":
            return state
        time.sleep(2)
    return "login-evidence-timeout"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args()
    try:
        outcome = verify(args.domain)
    except (OSError, subprocess.TimeoutExpired):
        outcome = "gateway-log-unavailable"
    print("OIDC login:", outcome)
    raise SystemExit(0 if outcome == "passed" else 1)
