#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Discover ready IPv4 node endpoints for the owned Envoy Gateway NodePort."""
import argparse
import ipaddress
import json
import subprocess
import sys


def _kubectl_json(arguments):
    result = subprocess.run(["kubectl", *arguments, "-o", "json"], text=True,
                           capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise RuntimeError("Kubernetes endpoint discovery failed")
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("Kubernetes returned invalid endpoint data") from None


def nodeport(service_list, nodes, namespace="envoy-gateway-system", gateway="atlas-monitoring-public"):
    if not isinstance(service_list, dict) or not isinstance(service_list.get("items"), list):
        raise ValueError("invalid Envoy Service list")
    owned = []
    for item in service_list["items"]:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        if (labels.get("gateway.envoyproxy.io/owning-gateway-namespace") == "monitoring"
                and labels.get("gateway.envoyproxy.io/owning-gateway-name") == gateway):
            owned.append(item)
    if len(owned) != 1:
        raise ValueError("expected exactly one Envoy Service for the monitoring Gateway")
    service = owned[0].get("spec", {})
    if service.get("type") != "NodePort":
        raise ValueError("monitoring Envoy Service is not NodePort")
    ports = [item for item in service.get("ports", [])
             if item.get("port") == 443 and item.get("protocol", "TCP") == "TCP"]
    if len(ports) != 1 or type(ports[0].get("nodePort")) is not int:
        raise ValueError("monitoring HTTPS NodePort is not assigned")
    port = ports[0]["nodePort"]
    if not 1 <= port <= 65535:
        raise ValueError("invalid monitoring HTTPS NodePort")
    items = nodes.get("items") if isinstance(nodes, dict) else None
    if not isinstance(items, list):
        raise ValueError("invalid Kubernetes node list")
    backends = set()
    for item in items:
        conditions = item.get("status", {}).get("conditions", [])
        if not any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions):
            continue
        addresses = [row.get("address") for row in item.get("status", {}).get("addresses", [])
                     if row.get("type") == "InternalIP"]
        if len(addresses) != 1:
            raise ValueError("each Ready node must have exactly one InternalIP")
        address = str(ipaddress.IPv4Address(addresses[0]))
        backends.add(f"{address}:{port}")
    if not backends:
        raise ValueError("no Ready Kubernetes nodes found")
    return {"nodePort": port, "backends": sorted(backends,
                                                   key=lambda value: tuple(map(int, value.rsplit(":", 1)[0].split("."))))}


def ensure_gateway_ready(endpoint_slices, gateway):
    conditions = gateway.get("status", {}).get("conditions", []) if isinstance(gateway, dict) else []
    if not any(row.get("type") == "Programmed" and row.get("status") == "True"
               for row in conditions):
        raise ValueError("Envoy Gateway data plane is not programmed")
    items = endpoint_slices.get("items") if isinstance(endpoint_slices, dict) else None
    if not isinstance(items, list):
        raise ValueError("invalid Envoy Gateway EndpointSlice list")
    ready = any(
        endpoint.get("addresses")
        and endpoint.get("conditions", {}).get("ready") is not False
        for item in items
        for endpoint in item.get("endpoints", [])
    )
    if not ready:
        raise ValueError("Envoy Gateway has no Ready data-plane endpoints")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="envoy-gateway-system")
    parser.add_argument("--gateway", default="atlas-monitoring-public")
    args = parser.parse_args()
    selector = ("gateway.envoyproxy.io/owning-gateway-namespace=monitoring,"
                f"gateway.envoyproxy.io/owning-gateway-name={args.gateway}")
    try:
        services = _kubectl_json(["-n", args.namespace, "get", "services", "-l", selector])
        nodes = _kubectl_json(["get", "nodes"])
        service_name = services["items"][0]["metadata"]["name"]
        endpoint_slices = _kubectl_json(["-n", args.namespace, "get", "endpointslices",
                                         "-l", f"kubernetes.io/service-name={service_name}"])
        gateway = _kubectl_json(["-n", "monitoring", "get", "gateway", args.gateway])
        ensure_gateway_ready(endpoint_slices, gateway)
        print(json.dumps(nodeport(services, nodes, args.namespace, args.gateway), separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
