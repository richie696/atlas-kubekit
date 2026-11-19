#!/usr/bin/env python3
"""Run a disposable HAProxy Prometheus exporter and verify real metrics over HTTP."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

HAPROXY_IMAGE = "haproxy:2.8-alpine"
PYTHON_IMAGE = "python:3.13-slim"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError("docker " + args[0] + " failed: " + result.stderr.strip())
    return result


def cleanup(suffix, network, container):
    failures = []
    for kind, name, template in (
        ("container", container, '{{ index .Config.Labels "atlas-kubekit.e2e" }}'),
        ("network", network, '{{ index .Labels "atlas-kubekit.e2e" }}'),
    ):
        found = docker(kind, "inspect", "--format", template, name, check=False)
        if found.returncode:
            continue
        if found.stdout.strip() != suffix:
            failures.append(name + " ownership mismatch")
            continue
        docker(kind, "rm", "-f", name, check=False) if kind == "container" else docker(kind, "rm", name, check=False)
        if docker(kind, "inspect", "--format", template, name, check=False).returncode == 0:
            failures.append(name + " cleanup not verified")
    if failures:
        raise RuntimeError("Cleanup failed: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, help="Type atlas-monitor-e2e")
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")

    suffix = uuid.uuid4().hex[:12]
    network, proxy = "atlas-lb-metrics-" + suffix, "atlas-haproxy-metrics-" + suffix
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-lb-metrics-", dir=temp_root) as directory:
            config = Path(directory) / "haproxy.cfg"
            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)
            network_info = json.loads(docker("network", "inspect", network).stdout)[0]
            cidr = network_info["IPAM"]["Config"][0]["Subnet"]
            config.write_text(f'''global
    log stdout format raw local0
defaults
    mode http
    timeout connect 2s
    timeout client 10s
    timeout server 10s
frontend prometheus-metrics
    bind *:8405
    acl metrics_source_allowed src {cidr}
    http-request deny if !metrics_source_allowed
    mode http
    option socket-stats
    http-request use-service prometheus-exporter if {{ path /metrics }}
    no log
frontend app
    bind *:8080
    default_backend app_nodes
backend app_nodes
    server unavailable 127.0.0.1:1 check
''')
            docker("run", "-d", "--name", proxy, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--pull=never",
                   "--network", network, "-v", str(config) + ":/usr/local/etc/haproxy/haproxy.cfg:ro",
                   HAPROXY_IMAGE, "-W", "-db", "-f", "/usr/local/etc/haproxy/haproxy.cfg")
            code = (
                "import time,urllib.request; url='http://" + proxy + ":8405/metrics'; "
                "deadline=time.monotonic()+25; data=b''; "
                "while_loop=False; "
                "exec('while time.monotonic()<deadline:\\n try:\\n  data=urllib.request.urlopen(url,timeout=2).read(); break\\n except Exception: time.sleep(.5)'); "
                "assert b'haproxy_frontend_current_sessions' in data, data[:1000]; "
                "assert b'haproxy_frontend_limit_sessions' in data, data[:1000]; "
                "assert b'haproxy_backend_active_servers' in data, data[:1000]; "
                "assert b'haproxy_backend_backup_servers' in data, data[:1000]; "
                "print('HTTP 200; frontend and backend metrics found')"
            )
            result = docker("run", "--pull=never", "--rm", "--network", network,
                            PYTHON_IMAGE, "python", "-c", code)
            print(result.stdout.strip())
    finally:
        cleanup(suffix, network, proxy)
        print("Owned temporary HAProxy container and network removed.")


if __name__ == "__main__":
    main()
