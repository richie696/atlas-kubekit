#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Managed by Atlas KubeKit: LB VIP metrics collector.
"""Expose local Keepalived state to HAProxy on loopback only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
import subprocess


PORT = 8406
NODE_PATTERN = re.compile(r"lb[1-9][0-9]*\Z")
INTERFACE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


def validate_identity(node, vip, interface):
    if not isinstance(node, str) or not NODE_PATTERN.fullmatch(node):
        raise ValueError("invalid LB node name")
    try:
        address = ipaddress.ip_address(vip)
    except (TypeError, ValueError):
        raise ValueError("invalid VIP address") from None
    if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("VIP must be a usable IPv4 address")
    if not isinstance(interface, str) or not INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError("invalid network interface")
    return str(address)


def _vip_list(node, vips, interface):
    if not isinstance(vips, (list, tuple)) or not 1 <= len(vips) <= 16:
        raise ValueError("one or more VIPs are required")
    normalized = []
    for vip in vips:
        value = validate_identity(node, vip, interface)
        if value not in normalized:
            normalized.append(value)
    return normalized


def render_metrics(node, vips, interface, keepalived_active, owned_vips):
    vips = _vip_list(node, vips, interface)
    owned_vips = set(owned_vips)
    active_lines = [
        "# HELP atlas_keepalived_active Whether Keepalived is active on this LB.\n"
        "# TYPE atlas_keepalived_active gauge\n"
    ]
    owner_lines = [
        "# HELP atlas_keepalived_vip_owner Whether this LB currently owns the configured VIP.\n"
        "# TYPE atlas_keepalived_vip_owner gauge\n"
    ]
    for vip in vips:
        labels = 'lb="%s",vip="%s",interface="%s"' % (node, vip, interface)
        active_lines.append("atlas_keepalived_active{%s} %d\n" % (labels, int(bool(keepalived_active))))
        owner_lines.append("atlas_keepalived_vip_owner{%s} %d\n" % (labels, int(vip in owned_vips)))
    return "".join(active_lines + owner_lines)


def collect_metrics(node, vips, interface, run=subprocess.run):
    vips = _vip_list(node, vips, interface)
    active = run(["systemctl", "is-active", "--quiet", "keepalived"],
                 check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    result = run(["ip", "-j", "-4", "addr", "show", "dev", interface],
                 check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("could not inspect configured interface")
    try:
        interfaces = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("interface address output is invalid") from None
    if not isinstance(interfaces, list) or len(interfaces) != 1:
        raise RuntimeError("configured interface is missing or ambiguous")
    present = {
        address.get("local") for address in interfaces[0].get("addr_info", [])
        if address.get("family") == "inet"
    }
    return render_metrics(node, vips, interface, active, present)


def serve(node, vips, interface):
    vips = _vip_list(node, vips, interface)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                body = collect_metrics(node, vips, interface).encode("utf-8")
            except (OSError, RuntimeError):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    try:
        serve(os.environ["ATLAS_LB_NAME"], os.environ["ATLAS_LB_VIPS"].split(","),
              os.environ["ATLAS_LB_INTERFACE"])
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
