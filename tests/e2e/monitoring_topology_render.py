# SPDX-License-Identifier: MIT
"""Render and inspect both monitoring topologies against a pinned Helm chart.

Usage: python3 tests/e2e/monitoring_topology_render.py --chart /path/to/kube-prometheus-stack.tgz
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.addons.monitoring_topology import (
    agent_values,
    center_values,
    verify_rendered_mode,
)
from scripts.addons.sources import manifest_loader, yaml_module


def render(chart, release, mode, values):
    yaml = yaml_module()
    with tempfile.TemporaryDirectory(prefix="atlas-monitoring-topology-") as temp:
        values_path = Path(temp) / f"{mode}.yaml"
        values_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        result = subprocess.run(
            ["helm", "template", release, str(chart), "--namespace", "monitoring",
             "--include-crds", "-f", str(values_path)],
            check=True, capture_output=True, text=True,
        )
    return result.stdout


def resources(manifest):
    yaml = yaml_module()
    return [item for item in yaml.load_all(manifest, Loader=manifest_loader(yaml))
            if isinstance(item, dict)]


def assert_rendered_chart(chart):
    center_manifest = render(chart, "atlas-center", "center", center_values("en"))
    center_summary = verify_rendered_mode("center", center_manifest)
    center_resources = resources(center_manifest)
    center_prometheus = [item for item in center_resources if item.get("kind") == "Prometheus"]
    if (len(center_prometheus) != 1
            or center_prometheus[0].get("spec", {}).get("enableRemoteWriteReceiver") is not True):
        raise AssertionError("rendered center does not expose the Prometheus Remote Write receiver")

    token_secret = "atlas-center-write-token"
    ca_secret = "atlas-center-ca"
    endpoint = "https://metrics.example.invalid/api/v1/write"
    agent_manifest = render(
        chart,
        "atlas-spoke",
        "agent",
        agent_values("render-probe", endpoint, token_secret, ca_secret),
    )
    agent_summary = verify_rendered_mode("agent", agent_manifest)
    agent_resources = resources(agent_manifest)
    agents = [item for item in agent_resources if item.get("kind") == "PrometheusAgent"]
    if len(agents) != 1:
        raise AssertionError("rendered spoke must contain exactly one PrometheusAgent")
    spec = agents[0].get("spec", {})
    remote_write = spec.get("remoteWrite", [])
    if len(remote_write) != 1:
        raise AssertionError("rendered PrometheusAgent must contain exactly one Remote Write target")
    target = remote_write[0]
    if target.get("url") != endpoint or spec.get("externalLabels", {}).get("cluster") != "render-probe":
        raise AssertionError("rendered Remote Write target lost its URL or cluster identity")
    if target.get("authorization", {}).get("credentials") != {"name": token_secret, "key": "token"}:
        raise AssertionError("rendered Remote Write auth must use a Kubernetes Secret reference")
    tls_ca = target.get("tlsConfig", {}).get("ca", {}).get("secret")
    if tls_ca != {"name": ca_secret, "key": "ca.crt"}:
        raise AssertionError("rendered Remote Write TLS must retain its CA Secret reference")

    center_only = {"grafana", "alertmanager", "loki"}
    workloads = [item for item in agent_resources
                 if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Service"}]
    leaked = []
    for item in workloads:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        names = [metadata.get("name", ""), *(
            value for key, value in labels.items()
            if key in {"app", "app.kubernetes.io/name", "app.kubernetes.io/component"}
        )]
        if any(component in str(name).casefold().split("-")
               for component in center_only for name in names):
            leaked.append(f"{item.get('kind')}/{metadata.get('name', '')}")
    if leaked:
        raise AssertionError("center-only workload rendered in spoke: " + ", ".join(leaked))
    if any(item.get("kind") in {"PrometheusRule", "Alertmanager", "AlertmanagerConfig"}
           for item in agent_resources):
        raise AssertionError("center rule/alert resources rendered in Agent mode")
    print(f"center topology rendered and verified: {center_summary}")
    print(f"Agent topology rendered and verified: {agent_summary}; no center-only resources")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", required=True, type=Path)
    args = parser.parse_args()
    if not args.chart.is_file():
        parser.error(f"Chart archive does not exist: {args.chart}")
    assert_rendered_chart(args.chart.resolve())


if __name__ == "__main__":
    main()
