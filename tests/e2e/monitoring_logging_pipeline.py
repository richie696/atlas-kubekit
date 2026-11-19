#!/usr/bin/env python3
"""Exercise real Alloy delivery, Loki rejection and Prometheus alert recovery."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.addons.logging_monitoring import COMPONENTS, logging_rules
from tests.e2e.monitoring_prometheus import cleanup_owned, docker

LOKI_IMAGE = "grafana/loki:3.7.7"
ALLOY_IMAGE = "grafana/alloy:v1.22.0"
PROMETHEUS_IMAGE = "prom/prometheus:v3.5.0"


def configuration(root):
    # JSON is a YAML subset. All backend data is internal to the test containers.
    loki = {
        "auth_enabled": False,
        "server": {"http_listen_port": 3100},
        "common": {
            "instance_addr": "127.0.0.1", "path_prefix": "/tmp/loki",
            "storage": {"filesystem": {"chunks_directory": "/tmp/loki/chunks", "rules_directory": "/tmp/loki/rules"}},
            "replication_factor": 1, "ring": {"kvstore": {"store": "inmemory"}},
        },
        "schema_config": {"configs": [{
            "from": "2024-01-01", "store": "tsdb", "object_store": "filesystem",
            "schema": "v13", "index": {"prefix": "index_", "period": "24h"},
        }]},
        "limits_config": {"max_line_size": "1KB", "max_line_size_truncate": False},
        "analytics": {"reporting_enabled": False},
    }
    alloy = """loki.source.file "fixture" {
  targets = [{__path__ = "/fixture/input.log", job = "atlas-fixture"}]
  forward_to = [loki.write.fixture.receiver]
}
loki.write "fixture" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
    batch_wait = "1s"
  }
}
"""
    spec = logging_rules(["loki", "alloy"], "zh_CN")["spec"]
    for rule in spec["groups"][0]["rules"]:
        # Production windows are exercised separately with promtool.
        rule["for"] = "4s"
        rule["expr"] = rule["expr"].replace("[5m]", "[20s]")
    scrapes = []
    for component, port in (("loki", 3100), ("alloy", 12345)):
        scrapes.append({
            "job_name": "atlas-logging-" + component,
            "static_configs": [{"targets": [f"{component}:{port}"], "labels": {"pod": component, "namespace": "logging"}}],
            "metric_relabel_configs": [{"action": "keep", "source_labels": ["__name__"],
                                        "regex": "|".join(COMPONENTS[component])}],
        })
    prom = {"global": {"scrape_interval": "2s", "evaluation_interval": "2s"},
            "rule_files": ["/fixture/rules.json"], "scrape_configs": scrapes}
    for name, body in (("loki.json", json.dumps(loki)), ("alloy.alloy", alloy),
                       ("rules.json", json.dumps(spec)), ("prom.json", json.dumps(prom)), ("input.log", "")):
        (root / name).write_text(body, encoding="utf-8")


def run(root, suffix, network, loki, alloy, prom):
    label = "atlas-kubekit.e2e=" + suffix
    docker("network", "create", "--internal", "--label", label, network)

    def start(container, alias, image, *args):
        docker("run", "-d", "--pull=never", "--label", label, "--name", container,
               "--network", network, "--network-alias", alias, "-v", f"{root}:/fixture:ro", image, *args)

    start(loki, "loki", LOKI_IMAGE, "-config.file=/fixture/loki.json")
    start(alloy, "alloy", ALLOY_IMAGE, "run", "--disable-reporting",
          "--server.http.listen-addr=0.0.0.0:12345", "--storage.path=/tmp/alloy", "/fixture/alloy.alloy")
    start(prom, "prometheus", PROMETHEUS_IMAGE, "--config.file=/fixture/prom.json", "--storage.tsdb.path=/prometheus")

    address = "http://127.0.0.1:9090"

    def samples(expression):
        result = docker("exec", prom, "/bin/promtool", "query", "instant", "--format=json",
                        address, expression, check=False)
        if result.returncode:
            return None
        return json.loads(result.stdout)

    def expect(expression, count, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observed = samples(expression)
            if observed is not None and len(observed) == count:
                return
            time.sleep(1)
        raise RuntimeError(f"{expression} did not converge to {count} samples")

    def append(line):
        with (root / "input.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def firing(name, reason=""):
        extra = f',reason="{reason}"' if reason else ""
        return f'ALERTS{{alertname="{name}",alertstate="firing"{extra}}}'

    expect('up{job=~"atlas-logging-.*"} == 1', 2)
    append("valid baseline log")
    expect('sum(loki_write_sent_entries_total{job="atlas-logging-alloy"}) >= 1', 1, timeout=50)
    expect('sum(loki_distributor_lines_received_total{job="atlas-logging-loki"}) >= 1', 1)
    # Establish a baseline for Loki's lazily created per-reason counter.
    append("initial rejected log " + "x" * 2048)
    expect('sum(loki_discarded_samples_total{job="atlas-logging-loki",reason="line_too_long"}) >= 1', 1)
    expect('sum(loki_write_dropped_entries_total{job="atlas-logging-alloy"}) >= 1', 1)
    append("second rejected log " + "x" * 2048)
    rejection = firing("AtlasLokiLogIngestionDiscarded", reason="line_too_long")
    dropped = firing("AtlasAlloyLogDeliveryDropped")
    expect(rejection, 1)
    expect(dropped, 1)
    print("Real Loki line_too_long rejection and Alloy delivery drops fired both alerts.", flush=True)

    append("valid recovery log")
    expect('sum(loki_write_sent_entries_total{job="atlas-logging-alloy"}) >= 2', 1)
    expect(rejection, 0)
    expect(dropped, 0)
    expect('up{job=~"atlas-logging-.*"} == 1', 2)
    print("Valid log delivery resumed and both alerts resolved with both scrapes healthy.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, choices=("atlas-monitor-e2e",))
    parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    network = "atlas-logging-" + suffix
    loki, alloy, prom = (f"{network}-{part}" for part in ("loki", "alloy", "prometheus"))
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-logging-pipeline-", dir=temp_root) as directory:
        root = Path(directory)
        root.chmod(0o755)
        configuration(root)
        try:
            run(root, suffix, network, loki, alloy, prom)
        except (RuntimeError, subprocess.SubprocessError):
            for name in (loki, alloy, prom):
                result = docker("logs", "--tail", "10", name, check=False)
                print(result.stderr[-2200:], file=sys.stderr)
            raise
        finally:
            cleanup_owned(suffix, network, (loki, alloy, prom))
    print("Temporary logging containers, network and fixture were removed.")


if __name__ == "__main__":
    main()
