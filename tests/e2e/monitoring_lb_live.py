#!/usr/bin/env python3
"""Exercise real HAProxy telemetry alerting and Keepalived VIP failover.

This destructive driver is deliberately pinned to disposable Atlas E2E labs.
It requires strict SSH host-key verification and explicit confirmation. Every
injected service failure is restored in finally.
"""
import argparse
from ipaddress import ip_address, ip_network
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode


LAB_PROFILES = {
    "retained-28-33": {
        "hosts": {"cp1": "10.20.1.28", "lb1": "10.20.1.32", "lb2": "10.20.1.33"},
        "vip_cidr": "10.20.1.31/32",
        "confirmation": "atlas-monitoring-lb-live-e2e-28-33",
    },
    "install-34-39": {
        "hosts": {"cp1": "10.20.1.34", "lb1": "10.20.1.37", "lb2": "10.20.1.38"},
        "vip_cidr": "10.20.1.39/32",
        "confirmation": "atlas-monitoring-lb-live-e2e-34-39",
    },
}
TARGET_ALERT = "AtlasLBTargetOrTelemetryMissing"
BACKEND_ALERT = "AtlasLBBackendUnavailableOrMetricsMissing"
VIP_ALERTS = {"AtlasLBKeepalivedInactive", "AtlasLBVipTelemetryMissing", "AtlasLBVipNoOwner",
              "AtlasLBVipMultipleOwners", "AtlasLBVipOwnershipDrift"}


class LabError(RuntimeError):
    pass


class Lab:
    def __init__(self, identity: Path, known_hosts: Path, timeout: int, profile: dict):
        self.identity = identity
        self.known_hosts = known_hosts
        self.timeout = timeout
        self.hosts = profile["hosts"]
        self.vip_cidr = profile["vip_cidr"]
        self.vip = self.vip_cidr.split("/", 1)[0]
        self._prometheus_api = None

    def ssh(self, node: str, command: str) -> str:
        result = subprocess.run(
            ["ssh", "-i", str(self.identity), "-o", "IdentitiesOnly=yes",
             "-o", "UserKnownHostsFile=" + str(self.known_hosts),
             "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=7", "ubuntu@" + self.hosts[node], command],
            check=False, text=True, capture_output=True, timeout=self.timeout,
        )
        if result.returncode:
            raise LabError("SSH command failed on " + node + ": " + result.stderr.strip())
        return result.stdout.strip()

    def prometheus_url(self) -> str:
        if self._prometheus_api:
            return self._prometheus_api
        pod_ip = self.ssh(
            "cp1",
            "sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring "
            "get pods -l app.kubernetes.io/name=prometheus "
            "-o jsonpath='{.items[0].status.podIP}'",
        )
        try:
            if ip_address(pod_ip) not in ip_network("10.244.0.0/16"):
                raise LabError("Prometheus Pod IP is outside the isolated lab Pod network")
        except ValueError as exc:
            raise LabError("could not discover a valid Prometheus Pod IP") from exc
        self._prometheus_api = "http://" + pod_ip + ":9090/api/v1/"
        return self._prometheus_api

    def prom(self, path: str) -> dict:
        output = self.ssh(
            "cp1", "curl -fsS --connect-timeout 2 --max-time 6 "
            + self.prometheus_url() + path,
        )
        payload = json.loads(output)
        if payload.get("status") != "success":
            raise LabError("Prometheus API returned an unsuccessful response")
        return payload["data"]

    def target_health(self, ip: str, endpoint: str) -> list[str]:
        targets = self.prom("targets").get("activeTargets", [])
        return [item.get("health") for item in targets
                if item.get("labels", {}).get("instance") == ip + ":8405"
                and item.get("labels", {}).get("metrics_endpoint") == endpoint]

    def lb_alerts(self) -> list[dict]:
        alerts = self.prom("alerts").get("alerts", [])
        return [item for item in alerts
                if item.get("labels", {}).get("alertname") in (TARGET_ALERT, BACKEND_ALERT)
                and (item.get("labels", {}).get("lb") == "lb2"
                     or item.get("labels", {}).get("node") == "lb2")]

    def vip_alerts(self, names=None) -> list[dict]:
        return [item for item in self.prom("alerts").get("alerts", [])
                if item.get("labels", {}).get("alertname") in (names or VIP_ALERTS)]

    def owns_vip(self, node: str) -> bool:
        return self.vip_cidr in self.ssh(node, "ip -4 -o addr show dev enp6s18")

    def vip_owner_metric(self, node: str, expected: int) -> bool:
        query = ('atlas_keepalived_vip_owner{job="atlas-lb-metrics",metrics_endpoint="vip",'
                 'lb="' + node + '",vip="' + self.vip + '"}')
        rows = self.prom("query?" + urlencode({"query": query})).get("result", [])
        return any(item.get("metric", {}).get("lb") == node
                   and item.get("value", [None, None])[1] == str(expected) for item in rows)

    def assert_vip_owner(self, expected: str) -> None:
        actual = [node for node in ("lb1", "lb2") if self.owns_vip(node)]
        if actual != [expected]:
            raise LabError("VIP owner is " + repr(actual) + "; expected only " + expected)

    def api_ready(self) -> bool:
        return self.ssh(
            "cp1", "sudo -n kubectl --kubeconfig=/etc/kubernetes/admin.conf get --raw=/readyz",
        ) == "ok"

    def wait_for(self, predicate, description: str, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        last_error = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (LabError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
                last_error = exc
            time.sleep(3)
        suffix = ": " + str(last_error) if last_error else ""
        raise LabError("timed out waiting for " + description + suffix)

    def check_preconditions(self) -> None:
        self.assert_vip_owner("lb1")
        if not self.api_ready():
            raise LabError("API VIP /readyz is not healthy")
        for node in ("lb1", "lb2"):
            if self.ssh(node, "systemctl is-active keepalived") != "active":
                raise LabError(node + " Keepalived is not active")
            if self.ssh(node, "systemctl is-active haproxy") != "active":
                raise LabError(node + " HAProxy is not active")
            if self.ssh(node, "sudo -n systemctl is-active atlas-lb-vip-metrics") != "active":
                raise LabError(node + " VIP metrics service is not active")
            if self.target_health(self.hosts[node], "vip") != ["up"]:
                raise LabError(node + " VIP metrics endpoint is not uniquely up")
        if self.target_health(self.hosts["lb2"], "haproxy") != ["up"]:
            raise LabError("lb2 HAProxy exporter target is not uniquely up")
        if self.lb_alerts() or self.vip_alerts():
            raise LabError("lb2 monitoring alerts are already active")

    def run_metrics_loss(self) -> None:
        print("Injecting standby lb2 HAProxy telemetry loss...")
        stopped = False
        try:
            stopped = True
            self.ssh("lb2", "sudo -n systemctl stop haproxy")
            self.wait_for(
                lambda: any(item.get("state") == "firing" for item in self.lb_alerts()),
                "a real Prometheus lb2 telemetry alert", 180,
            )
            self.assert_vip_owner("lb1")
            if not self.api_ready():
                raise LabError("primary API became unhealthy during standby telemetry test")
            fired = sorted({item["labels"]["alertname"] for item in self.lb_alerts()
                            if item.get("state") == "firing"})
            print("PASS: real lb2 HAProxy loss fired " + ", ".join(fired)
                  + "; primary VIP/API remained healthy.")
        finally:
            if stopped:
                self.ssh("lb2", "sudo -n systemctl start haproxy")
                self.wait_for(
                    lambda: self.target_health(self.hosts["lb2"], "haproxy") == ["up"] and not self.lb_alerts(),
                    "lb2 exporter recovery and alert resolution", 180,
                )
                self.assert_vip_owner("lb1")
                if not self.api_ready():
                    raise LabError("API did not remain healthy after HAProxy recovery")
                print("Cleanup PASS: HAProxy/exporter recovered, alert resolved, lb1 kept VIP.")

    def run_vip_failover(self) -> None:
        print("Injecting primary lb1 Keepalived stop...")
        stopped = False
        try:
            stopped = True
            self.ssh("lb1", "sudo -n systemctl stop keepalived")
            self.wait_for(lambda: self.owns_vip("lb2") and not self.owns_vip("lb1"),
                          "exclusive VIP failover to lb2", 30)
            if not self.api_ready():
                raise LabError("API VIP /readyz failed after VIP failover")
            for node in ("lb1", "lb2"):
                output = self.ssh(
                    "cp1", "curl -fsS --connect-timeout 2 --max-time 5 http://"
                    + self.hosts[node] + ":8405/metrics | grep -m1 '^haproxy_backend_active_servers'",
                )
                if not output:
                    raise LabError(node + " exporter returned no backend metric")
            print("PASS: VIP moved exclusively to lb2; API and both exporters stayed healthy.")
        finally:
            if stopped:
                self.ssh("lb1", "sudo -n systemctl start keepalived")
                self.wait_for(lambda: self.owns_vip("lb1") and not self.owns_vip("lb2"),
                              "VIP restoration to lb1", 45)
                if not self.api_ready():
                    raise LabError("API VIP /readyz failed after VIP restoration")
                print("Cleanup PASS: Keepalived active; VIP exclusively restored to lb1.")

    def run_vip_no_owner(self) -> None:
        print("Injecting isolated VIP no-owner condition by stopping Keepalived on both LBs...")
        touched = []
        try:
            self.ssh("lb1", "sudo -n systemctl stop keepalived")
            touched.append("lb1")
            self.wait_for(lambda: self.owns_vip("lb2") and not self.owns_vip("lb1"),
                          "VIP failover to lb2 before the no-owner injection", 30)
            self.ssh("lb2", "sudo -n systemctl stop keepalived")
            touched.append("lb2")
            self.wait_for(lambda: any(item.get("state") == "firing"
                                      and item.get("labels", {}).get("alertname") == "AtlasLBVipNoOwner"
                                      for item in self.vip_alerts()),
                          "real Prometheus VIP no-owner alert", 180)
            print("PASS: real Prometheus detected that neither registered LB owns the VIP.")
        finally:
            # Restore the preferred owner first; both actions are idempotent.
            self.ssh("lb1", "sudo -n systemctl start keepalived")
            self.ssh("lb2", "sudo -n systemctl start keepalived")
            self.wait_for(lambda: self.owns_vip("lb1") and not self.owns_vip("lb2"),
                          "VIP ownership restoration to lb1", 60)
            self.wait_for(lambda: not self.vip_alerts({"AtlasLBVipNoOwner", "AtlasLBVipMultipleOwners"}),
                          "VIP ownership alerts resolution", 180)
            if not self.api_ready():
                raise LabError("API VIP /readyz did not recover after no-owner injection")
            print("Cleanup PASS: both Keepalived services active, VIP on lb1, and VIP alerts resolved.")

    def run_vip_multiple_owners(self) -> None:
        print("Injecting an extra VIP address on lb2 to validate duplicate-owner detection...")
        added = False
        try:
            self.ssh("lb2", "sudo -n ip address add " + self.vip_cidr + " dev enp6s18")
            added = True
            self.wait_for(lambda: any(item.get("state") == "firing"
                                      and item.get("labels", {}).get("alertname") == "AtlasLBVipMultipleOwners"
                                      for item in self.vip_alerts()),
                          "real Prometheus VIP multiple-owner alert", 180)
            owners = [node for node in ("lb1", "lb2") if self.owns_vip(node)]
            if owners != ["lb1", "lb2"]:
                raise LabError("duplicate VIP injection did not produce two Linux address owners")
            print("PASS: real Prometheus detected the VIP address on both LB nodes.")
        finally:
            if added:
                self.ssh("lb2", "sudo -n ip address del " + self.vip_cidr + " dev enp6s18 || true")
            self.wait_for(lambda: self.owns_vip("lb1") and not self.owns_vip("lb2"),
                          "duplicate VIP removal and lb1 ownership", 60)
            self.wait_for(lambda: not self.vip_alerts({"AtlasLBVipMultipleOwners", "AtlasLBVipNoOwner"}),
                          "VIP multiple-owner alert resolution", 180)
            if not self.api_ready():
                raise LabError("API VIP /readyz did not recover after duplicate-owner injection")
            print("Cleanup PASS: injected VIP address removed and alerts resolved.")

    def run_vip_drift(self) -> None:
        print("Injecting repeated primary/backup transitions to validate the live drift threshold...")
        try:
            for cycle in range(2):
                self.ssh("lb1", "sudo -n systemctl stop keepalived")
                self.wait_for(lambda: self.owns_vip("lb2") and not self.owns_vip("lb1"),
                              "VIP transition to lb2 in drift cycle " + str(cycle + 1), 45)
                self.wait_for(lambda: self.vip_owner_metric("lb1", 0)
                              and self.vip_owner_metric("lb2", 1),
                              "Prometheus scrape of the exclusive lb2 owner state", 90)
                if not self.api_ready():
                    raise LabError("API VIP /readyz failed during drift cycle " + str(cycle + 1))
                self.ssh("lb1", "sudo -n systemctl start keepalived")
                self.wait_for(lambda: self.owns_vip("lb1") and not self.owns_vip("lb2"),
                              "VIP transition back to lb1 in drift cycle " + str(cycle + 1), 60)
                self.wait_for(lambda: self.vip_owner_metric("lb1", 1)
                              and self.vip_owner_metric("lb2", 0),
                              "Prometheus scrape of the restored lb1 owner state", 90)
            self.wait_for(lambda: any(item.get("state") == "firing"
                                      and item.get("labels", {}).get("alertname") == "AtlasLBVipOwnershipDrift"
                                      for item in self.vip_alerts()),
                          "real Prometheus repeated-VIP-drift alert", 240)
            self.assert_vip_owner("lb1")
            if not self.api_ready():
                raise LabError("API VIP /readyz failed after drift test")
            print("PASS: repeated Keepalived transitions fired the configured drift alert; lb1 owns VIP and API is healthy.")
        finally:
            self.ssh("lb1", "sudo -n systemctl start keepalived")
            self.ssh("lb2", "sudo -n systemctl start keepalived")
            self.wait_for(lambda: self.owns_vip("lb1") and not self.owns_vip("lb2"),
                          "VIP cleanup to preferred lb1 owner", 60)
        # `changes(...[10m])` intentionally retains evidence until the last transition ages out.
        self.wait_for(lambda: not any(item.get("labels", {}).get("alertname") == "AtlasLBVipOwnershipDrift"
                                      for item in self.vip_alerts()),
                      "drift alert resolution after the 10-minute observation window", 720)
        self.assert_vip_owner("lb1")
        print("Cleanup PASS: drift alert resolved after the observation window; VIP remains on lb1.")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--lab-profile", required=True, choices=tuple(LAB_PROFILES))
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--identity", type=Path, default=Path.home() / ".ssh/id_ed25519")
    parser.add_argument("--scenario", choices=("all", "metrics-loss", "vip-failover", "vip-no-owner",
                                                "vip-multiple-owners", "vip-drift"), default="all")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    if args.confirm != LAB_PROFILES[args.lab_profile]["confirmation"]:
        parser.error("refusing to run without the exact isolated-lab confirmation")
    if not args.known_hosts.is_file() or not args.identity.is_file():
        parser.error("SSH identity and pre-verified known-hosts file must exist")
    if args.timeout < 5 or args.timeout > 60:
        parser.error("--timeout must be between 5 and 60 seconds")
    return args


def main():
    args = parse_args()
    lab = Lab(args.identity, args.known_hosts, args.timeout, LAB_PROFILES[args.lab_profile])
    lab.check_preconditions()
    scenarios = ("metrics-loss", "vip-failover", "vip-no-owner", "vip-multiple-owners", "vip-drift") \
        if args.scenario == "all" else (args.scenario,)
    for scenario in scenarios:
        if scenario == "metrics-loss":
            lab.run_metrics_loss()
        elif scenario == "vip-failover":
            lab.run_vip_failover()
        elif scenario == "vip-no-owner":
            lab.run_vip_no_owner()
        elif scenario == "vip-drift":
            lab.run_vip_drift()
        else:
            lab.run_vip_multiple_owners()
    print("Live LB monitoring E2E: PASS")


if __name__ == "__main__":
    main()
