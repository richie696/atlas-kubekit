#!/usr/bin/env python3
"""Run a real Prometheus -> Alertmanager -> relay pipeline with a fake vendor."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.rules import node_readiness_rule
from scripts.operations.monitoring.prometheus import collect
from tests.e2e.monitoring_alertmanager import CONFIG, RECEIVER, wait_log
from tests.e2e.monitoring_prometheus import cleanup_owned, docker

EXPORTER = '''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  state=Path('/tmp/test/state').read_text().strip()
  body=('kube_node_status_condition{node="cp1",condition="Ready",status="true"} '+state+'\\n').encode()
  self.send_response(200); self.send_header('Content-Type','text/plain; version=0.0.4'); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''
CONFIG_PROM = '''global:
  scrape_interval: 2s
  evaluation_interval: 1s
rule_files: ["/test/rules.json"]
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["manager:9093"]
scrape_configs:
  - job_name: atlas-e2e
    static_configs:
      - targets: ["exporter:8000"]
'''


def wait_ready(port):
    deadline = time.monotonic() + 30
    while True:
        try:
            with urlopen("http://127.0.0.1:" + port + "/-/ready", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Prometheus failed to start")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    network = "atlas-pipeline-" + suffix
    manager, relay, exporter, prom = ("atlas-pipeline-am-" + suffix, "atlas-pipeline-relay-" + suffix,
                                      "atlas-pipeline-exporter-" + suffix, "atlas-pipeline-prom-" + suffix)
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-pipeline-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "state").write_text("1")
            (root / "exporter.py").write_text(EXPORTER)
            (root / "receiver.py").write_text(RECEIVER)
            (root / "alertmanager.yml").write_text(CONFIG)
            (root / "prometheus.yml").write_text(CONFIG_PROM)
            rule = node_readiness_rule("monitoring", "stack", ["cp1"], 1, 120, "warning",
                                       "节点 cp1 未就绪或监控数据缺失", "检查节点和监控采集状态。")
            (root / "rules.json").write_text(json.dumps(rule["spec"]))
            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)
            docker("run", "-d", "--pull=never", "--name", manager, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "--network-alias", "manager", "-v", str(root / "alertmanager.yml") + ":/etc/alertmanager/alertmanager.yml:ro",
                   "prom/alertmanager:v0.28.1", "--config.file=/etc/alertmanager/alertmanager.yml", "--cluster.listen-address=")
            docker("run", "-d", "--pull=never", "--name", relay, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", "container:" + manager, "-v", str(root / "receiver.py") + ":/tmp/receiver.py:ro",
                   "-v", str(ROOT) + ":/work:ro", "python:3.13-slim", "python", "-u", "/tmp/receiver.py")
            docker("run", "-d", "--pull=never", "--name", exporter, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "--network-alias", "exporter", "-v", str(root) + ":/tmp/test:ro",
                   "python:3.13-slim", "python", "-u", "/tmp/test/exporter.py")
            docker("run", "-d", "--pull=never", "--name", prom, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "-p", "127.0.0.1::9090", "-v", str(root) + ":/test:ro",
                   "prom/prometheus:v3.5.0", "--config.file=/test/prometheus.yml")
            port = docker("port", prom, "9090/tcp").stdout.strip().rsplit(":", 1)[1]
            wait_ready(port)
            deadline = time.monotonic() + 30
            while collect("http://127.0.0.1:" + port, ["cp1"], 120).get("nodes") != [{"node": "cp1", "state": "ready"}]:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Prometheus did not collect the simulated healthy node")
                time.sleep(1)
            (root / "state").write_text("0")
            deadline = time.monotonic() + 30
            while collect("http://127.0.0.1:" + port, ["cp1"], 120).get("nodes") != [{"node": "cp1", "state": "not-ready"}]:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Prometheus did not collect the simulated node failure")
                time.sleep(1)
            try:
                wait_log(relay, "firing")
            except RuntimeError:
                for container in (prom, manager, relay):
                    result = docker("logs", container, check=False)
                    print(container + ": " + result.stdout[-5000:] + result.stderr[-5000:], file=sys.stderr)
                raise
            if "RETRY firing" not in docker("logs", relay).stdout:
                raise RuntimeError("relay retry was not exercised")
            (root / "state").write_text("1")
            wait_log(relay, "resolved")
            print("Real Prometheus rule -> Alertmanager -> authenticated relay -> fake vendor passed")
    finally:
        cleanup_owned(suffix, network, (relay, prom, exporter, manager))
        print("Owned temporary containers and network cleaned up.")


if __name__ == "__main__":
    main()
