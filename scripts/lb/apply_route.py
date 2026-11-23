#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Apply one approved HAProxy route through the LB manager's two-node transaction."""
import argparse
import ipaddress
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lb.manager import App


def load_request(path):
    request = json.loads(Path(path).read_text())
    if not isinstance(request, dict) or set(request) != {"sshUser", "nodes", "vip", "route"}:
        raise ValueError("invalid LB route request")
    user = request["sshUser"]
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise ValueError("invalid LB SSH user")
    nodes = request["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 2:
        raise ValueError("exactly two managed LB nodes are required")
    normalized = []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"name", "ip"}:
            raise ValueError("invalid LB node")
        if node["name"] not in ("lb1", "lb2"):
            raise ValueError("LB nodes must be named lb1 and lb2")
        address = str(ipaddress.IPv4Address(node["ip"]))
        normalized.append({"name": node["name"], "ip": address})
    if {item["name"] for item in normalized} != {"lb1", "lb2"} or len({item["ip"] for item in normalized}) != 2:
        raise ValueError("LB pair must have unique lb1/lb2 addresses")
    vip = str(ipaddress.IPv4Address(request["vip"]))
    route = request["route"]
    if (not isinstance(route, dict)
            or set(route) != {"name", "mode", "listenPort", "bind", "backends", "websocket"}
            or route["name"] != "monitoring-https" or route["mode"] != "tcp"
            or route["listenPort"] != 443 or route["bind"] != "*"
            or route["websocket"] is not False or not isinstance(route["backends"], list)
            or not route["backends"]):
        raise ValueError("invalid monitoring HTTPS route")
    backends = []
    for backend in route["backends"]:
        host, separator, raw_port = backend.rpartition(":") if isinstance(backend, str) else ("", "", "")
        if not separator or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ValueError("invalid monitoring NodePort backend")
        backends.append(f"{ipaddress.IPv4Address(host)}:{int(raw_port)}")
    if len(backends) != len(set(backends)):
        raise ValueError("duplicate monitoring NodePort backend")
    route = {**route, "backends": backends}
    return {"sshUser": user, "nodes": normalized, "vip": vip, "route": route}


def apply(app, request):
    app.config = {"sshUser": request["sshUser"], "managed": request["nodes"], "external": []}
    app.ensure_nodes()
    snapshots = app.get_states()
    if any(status.get("transactionOwner") for _, status in snapshots):
        recover_pending_transaction(app, request, snapshots)
        snapshots = app.get_states()
    for node, status in snapshots:
        if status.get("hostname") != node["name"]:
            raise RuntimeError(f"LB identity mismatch for {node['ip']}: expected {node['name']}")
    states = [status.get("state", {"vips": [], "routes": []}) for _, status in snapshots]
    canonical = json.dumps(states[0], sort_keys=True)
    if any(json.dumps(state, sort_keys=True) != canonical for state in states[1:]):
        raise RuntimeError(app.t("lb.error.state_mismatch"))
    if request["vip"] not in states[0].get("vips", []):
        raise RuntimeError("application VIP is not registered in both LB nodes")
    desired = request["route"]
    last_error = None
    for attempt in range(3):
        try:
            for node in request["nodes"]:
                result = app.ssh(node, {"action": "probe-backends", "backends": desired["backends"]})
                if result.get("reachable") != desired["backends"]:
                    raise RuntimeError(f"backend preflight returned incomplete results on {node['name']}")
            last_error = None
            break
        except RuntimeError as error:
            last_error = error
            if attempt < 2:
                print("Gateway backend preflight is not ready; retrying both LB nodes in 2 seconds.")
                time.sleep(2)
    if last_error is not None:
        raise last_error
    existing = next((item for item in states[0].get("routes", [])
                     if item.get("name") == desired["name"]), None)
    if existing == desired:
        print("Monitoring HTTPS HAProxy route already matches the desired state; no change needed.")
        return 0
    for item in states[0].get("routes", []):
        if item.get("bind") == "*" and item.get("listenPort") == 443 and item.get("name") != desired["name"]:
            raise RuntimeError("HAProxy port 443 is already owned by another managed route")
    print("LB route preview:")
    for node in request["nodes"]:
        print(f"  {node['name']} ({node['ip']})")
    print(f"  VIP {request['vip']}:443 -> " + ", ".join(desired["backends"]))

    def mutate(state):
        rows = [row for row in state["routes"] if row["name"] != desired["name"]]
        rows.append(desired)
        rows.sort(key=lambda row: row["name"])
        state["routes"] = rows

    app.change_state(mutate)
    committed = app.get_states()
    if any(desired not in status.get("state", {}).get("routes", []) for _, status in committed):
        raise RuntimeError("LB route change was cancelled or did not converge on both nodes")
    return 0


def _without_route(state, route_name):
    if not isinstance(state, dict) or set(state) != {"vips", "routes"}:
        raise RuntimeError("LB transaction contains invalid state")
    return {"vips": state["vips"],
            "routes": [row for row in state["routes"] if row.get("name") != route_name]}


def recover_pending_transaction(app, request, snapshots):
    """Recover only a pending transaction that changed this route and nothing else."""
    pending = []
    for node, status in snapshots:
        owner = status.get("transactionOwner")
        if not owner:
            continue
        info = app.ssh(node, {"action": "transaction-info"})
        if not info.get("pending") or info.get("token") != owner:
            raise RuntimeError("LB transaction recovery evidence is incomplete")
        old_state, new_state = info.get("oldState"), info.get("newState")
        route_name = request["route"]["name"]
        if (_without_route(old_state, route_name) != _without_route(new_state, route_name)
                or request["vip"] not in new_state.get("vips", [])
                or request["route"] not in new_state.get("routes", [])):
            raise RuntimeError("A pending LB transaction contains unrelated changes; recover it in the LB manager")
        pending.append((node, info))
    if len({info["token"] for _, info in pending}) != 1:
        raise RuntimeError("LB nodes contain different pending transactions; recover them in the LB manager")

    candidate = pending[0][1]["newState"]
    if any(info["newState"] != candidate for _, info in pending):
        raise RuntimeError("LB pending transactions disagree; recover them in the LB manager")
    all_committed = all(info.get("committed") is True for _, info in pending)
    non_pending_match = all(
        status.get("transactionOwner") or status.get("state") == candidate
        for _, status in snapshots
    )
    if all_committed and non_pending_match:
        action = "finish"
    else:
        action = "rollback"
    for node, info in pending:
        app.ssh(node, {"action": action, "token": info["token"]})
    recovered = app.get_states()
    states = [json.dumps(status.get("state", {}), sort_keys=True) for _, status in recovered]
    if not states or any(state != states[0] for state in states[1:]):
        raise RuntimeError("LB transaction recovery did not align both nodes; inspect the LB manager")
    print("Recovered pending monitoring HTTPS LB transaction by " + action + ".")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="Private JSON request assembled by menu 19")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--ssh-identity")
    parser.add_argument("--ssh-config")
    args = parser.parse_args(argv)
    try:
        request = load_request(args.request)
        return apply(App(args.lang, 3, args.ssh_identity, args.ssh_config), request)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
