#!/usr/bin/env python3
"""Verify Alertmanager grouping, timing, and resolution against a live cluster."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Receiver(BaseHTTPRequestHandler):
    events = []
    lock = threading.Lock()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        with self.lock:
            self.events.append({"received_at": time.monotonic(), "body": body})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        return


def kubectl(kubeconfig, args, value=None, timeout=45):
    command = ["kubectl", "--kubeconfig=" + kubeconfig, *args]
    result = subprocess.run(command, input=json.dumps(value).encode() if value is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError("kubectl operation failed: " + " ".join(args[:3]))
    return result.stdout


def api(base, method, endpoint, value=None):
    request = Request(base + endpoint,
                      data=json.dumps(value).encode() if value is not None else None,
                      headers={"Content-Type": "application/json"}, method=method)
    with urlopen(request, timeout=10) as response:
        raw = response.read(4 * 1024 * 1024)
    return json.loads(raw.decode()) if raw else None


def firing_groups(events, token):
    result = {}
    for event in events:
        body = event["body"]
        if body.get("status") != "firing":
            continue
        alerts = [a for a in body.get("alerts", [])
                  if a.get("labels", {}).get("atlas_e2e_id") == token]
        if not alerts:
            continue
        labels = body.get("groupLabels", {})
        key = (labels.get("cluster"), labels.get("team"), labels.get("alertname"))
        result[key] = max(result.get(key, 0), len(alerts))
    return result


def wait_for(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--receiver-address", required=True,
                        help="IPv4 address of this host reachable from Alertmanager Pods")
    parser.add_argument("--kubeconfig", default="/etc/kubernetes/admin.conf")
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--release", default="kube-prometheus-stack")
    parser.add_argument("--service", default="kube-prometheus-stack-alertmanager")
    parser.add_argument("--port-name", default="http-web")
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e-grouping":
        parser.error("Explicit confirmation atlas-monitor-e2e-grouping is required")
    try:
        receiver_ip = ipaddress.ip_address(args.receiver_address)
    except ValueError:
        parser.error("--receiver-address must be an IP address")
    if receiver_ip.version != 4 or receiver_ip.is_loopback or receiver_ip.is_unspecified:
        parser.error("--receiver-address must be a reachable, non-loopback IPv4 address")

    suffix = uuid.uuid4().hex[:10]
    token = "atlas-group-e2e-" + suffix
    name = "atlas-group-e2e-" + suffix
    alertname = "AtlasAlertGroupingE2E" + suffix
    service_path = ("/api/v1/namespaces/" + args.namespace + "/services/http:" + args.service + ":" +
                    args.port_name + "/proxy")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        proxy_port = sock.getsockname()[1]
    proxy = subprocess.Popen(["kubectl", "--kubeconfig=" + args.kubeconfig, "proxy",
                              "--address=127.0.0.1", "--port=" + str(proxy_port)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    receiver = ThreadingHTTPServer((str(receiver_ip), 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    receiver_url = "http://" + str(receiver_ip) + ":" + str(receiver.server_address[1]) + "/"
    base = "http://127.0.0.1:" + str(proxy_port) + service_path
    created = False
    alerts = []
    silence_id = None

    try:
        wait_for(lambda: proxy.poll() is None and proxy_ready(proxy_port), 15,
                 "kubectl proxy did not become ready")
        manifest = {
            "apiVersion": "monitoring.coreos.com/v1alpha1",
            "kind": "AlertmanagerConfig",
            "metadata": {"name": name, "namespace": args.namespace,
                         "labels": {"release": args.release,
                                    "atlas-kubekit.io/managed-by": "monitoring",
                                    "atlas-kubekit.io/e2e": token}},
            "spec": {
                "route": {
                    "receiver": "grouping-webhook",
                    "matchers": [{"name": "atlas_e2e_id", "value": token, "matchType": "="}],
                    "groupBy": ["cluster", "alertname", "team"],
                    "groupWait": "2s", "groupInterval": "2s", "repeatInterval": "30s",
                },
                "receivers": [{"name": "grouping-webhook",
                               "webhookConfigs": [{"url": receiver_url, "sendResolved": True}]}],
            },
        }
        kubectl(args.kubeconfig, ["create", "--dry-run=server", "-f", "-"], manifest)
        kubectl(args.kubeconfig, ["create", "-f", "-"], manifest)
        created = True
        status_path = "/api/v2/status"
        wait_for(lambda: token in str(api(base, "GET", status_path).get("config", {}).get("original", "")),
                 60, "Alertmanager did not load the temporary grouping route")

        starts = datetime.now(timezone.utc) - timedelta(seconds=1)
        ends = datetime.now(timezone.utc) + timedelta(minutes=2)
        def make_alert(cluster, team, pod):
            return {"labels": {"alertname": alertname, "severity": "warning",
                               "namespace": args.namespace, "cluster": cluster, "team": team,
                               "pod": pod, "atlas_e2e_id": token},
                    "annotations": {"summary": "Atlas grouping E2E"},
                    "startsAt": starts.isoformat(), "endsAt": ends.isoformat()}

        alerts = [make_alert("atlas-e2e-a", "platform", "pod-a1"),
                  make_alert("atlas-e2e-a", "platform", "pod-a2"),
                  make_alert("atlas-e2e-b", "platform", "pod-b1"),
                  make_alert("atlas-e2e-a", "data", "pod-a3")]
        sent_at = time.monotonic()
        api(base, "POST", "/api/v2/alerts", alerts)
        expected = {(cluster, team, alertname): count for cluster, team, count in (
            ("atlas-e2e-a", "platform", 2), ("atlas-e2e-b", "platform", 1),
            ("atlas-e2e-a", "data", 1))}
        wait_for(lambda: all(firing_groups(Receiver.events, token).get(key, 0) == count
                             for key, count in expected.items()),
                 25, "initial Alertmanager groups or group_wait delivery were not observed")
        first_events = [event for event in Receiver.events
                        if event["body"].get("status") == "firing" and any(
                            alert.get("labels", {}).get("atlas_e2e_id") == token
                            for alert in event["body"].get("alerts", []))]
        first_delay = min(event["received_at"] for event in first_events) - sent_at
        if not 1.2 <= first_delay < 20:
            raise RuntimeError("group_wait timing outside the tested bound")

        update = make_alert("atlas-e2e-a", "platform", "pod-a4")
        interval_start = time.monotonic()
        alerts.append(update)
        api(base, "POST", "/api/v2/alerts", [update])
        wait_for(lambda: firing_groups(Receiver.events, token).get(
                     ("atlas-e2e-a", "platform", alertname), 0) == 3,
                 20, "group_interval update containing the new alert was not delivered")
        updated = [event for event in Receiver.events
                   if event["body"].get("status") == "firing" and
                   len([alert for alert in event["body"].get("alerts", [])
                        if alert.get("labels", {}).get("atlas_e2e_id") == token]) >= 3]
        update_delay = min(event["received_at"] for event in updated) - interval_start
        if not 1.2 <= update_delay < 20:
            raise RuntimeError("group_interval timing outside the tested bound")

        resolved_at = datetime.now(timezone.utc).isoformat()
        for alert in alerts:
            alert["endsAt"] = resolved_at
        api(base, "POST", "/api/v2/alerts", alerts)
        wait_for(lambda: any(event["body"].get("status") == "resolved" and any(
            alert.get("labels", {}).get("atlas_e2e_id") == token
            for alert in event["body"].get("alerts", [])) for event in Receiver.events),
                 20, "resolved notification was not delivered")
        wait_for(lambda: not any(alert.get("labels", {}).get("atlas_e2e_id") == token
                                 for alert in api(base, "GET", "/api/v2/alerts")),
                 20, "synthetic alerts remained active after resolution")

        # Alertmanager accepts a minimum one-minute silence through the supported
        # 06 workflow. Verify it suppresses a firing group, expires on its own,
        # and allows the notification and subsequent recovery through.
        silence_created_at = time.monotonic()
        silence_ends_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        silence = api(base, "POST", "/api/v2/silences", {
            "matchers": [
                {"name": "atlas_e2e_id", "value": token, "isEqual": True, "isRegex": False},
                {"name": "severity", "value": "warning", "isEqual": True, "isRegex": False},
            ],
            "startsAt": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "endsAt": silence_ends_at.isoformat(),
            "createdBy": "Atlas KubeKit grouping E2E",
            "comment": "Temporary one-minute automatic-expiry verification",
        })
        silence_id = silence.get("silenceID") if isinstance(silence, dict) else None
        if not isinstance(silence_id, str) or not silence_id:
            raise RuntimeError("Alertmanager did not return the temporary silence ID")
        silence_alert = make_alert("atlas-e2e-silence", "platform", "pod-silence")
        alerts.append(silence_alert)
        api(base, "POST", "/api/v2/alerts", [silence_alert])
        time.sleep(8)
        if any(event["received_at"] >= silence_created_at and
               event["body"].get("status") == "firing" and any(
                   item.get("labels", {}).get("atlas_e2e_id") == token and
                   item.get("labels", {}).get("cluster") == "atlas-e2e-silence"
                   for item in event["body"].get("alerts", [])) for event in Receiver.events):
            raise RuntimeError("Alertmanager notified a matching alert while the silence was active")
        wait_for(lambda: any(event["received_at"] >= silence_created_at and
                             event["body"].get("status") == "firing" and any(
                                 item.get("labels", {}).get("atlas_e2e_id") == token and
                                 item.get("labels", {}).get("cluster") == "atlas-e2e-silence"
                                 for item in event["body"].get("alerts", []))
                             for event in Receiver.events),
                 100, "firing notification was not released after automatic silence expiry")
        silence_delivery = next(event for event in Receiver.events
                                if event["received_at"] >= silence_created_at and
                                event["body"].get("status") == "firing" and any(
                                    item.get("labels", {}).get("atlas_e2e_id") == token and
                                    item.get("labels", {}).get("cluster") == "atlas-e2e-silence"
                                    for item in event["body"].get("alerts", [])))
        if silence_delivery["received_at"] < silence_created_at + 55:
            raise RuntimeError("silenced alert was delivered before its one-minute expiry")
        silence_alert["endsAt"] = datetime.now(timezone.utc).isoformat()
        api(base, "POST", "/api/v2/alerts", [silence_alert])
        wait_for(lambda: any(event["received_at"] >= silence_created_at and
                             event["body"].get("status") == "resolved" and any(
                                 item.get("labels", {}).get("atlas_e2e_id") == token and
                                 item.get("labels", {}).get("cluster") == "atlas-e2e-silence"
                                 for item in event["body"].get("alerts", []))
                             for event in Receiver.events),
                 30, "resolved notification was not delivered after the silence expired")
        printable_groups = {"/".join(key): count for key, count in expected.items()}
        print(json.dumps({"result": "passed", "initial_groups": printable_groups,
                          "group_wait_seconds": round(first_delay, 2),
                          "group_interval_seconds": round(update_delay, 2),
                          "silence_seconds": 60,
                          "silence_release_seconds": round(silence_delivery["received_at"] - silence_created_at, 2),
                          "resolved_notifications": sum(e["body"].get("status") == "resolved"
                                                         for e in Receiver.events)}, sort_keys=True))
    finally:
        cleanup_errors = []
        if alerts:
            end = datetime.now(timezone.utc).isoformat()
            for alert in alerts:
                alert["endsAt"] = end
            try:
                api(base, "POST", "/api/v2/alerts", alerts)
            except Exception as error:
                cleanup_errors.append("resolve test alerts: " + type(error).__name__)
        if silence_id:
            try:
                api(base, "DELETE", "/api/v2/silence/" + quote(silence_id, safe=""))
            except Exception as error:
                cleanup_errors.append("delete test silence: " + type(error).__name__)
        if created:
            try:
                kubectl(args.kubeconfig, ["delete", "alertmanagerconfig", name,
                                          "--namespace", args.namespace, "--wait=true"], timeout=60)
            except Exception as error:
                cleanup_errors.append("delete test AlertmanagerConfig: " + type(error).__name__)
        receiver.shutdown()
        receiver.server_close()
        try:
            wait_for(lambda: not any(
                alert.get("labels", {}).get("atlas_e2e_id") == token
                for alert in api(base, "GET", "/api/v2/alerts")),
                15, "test alerts remained active after cleanup")
            if silence_id and any(
                    item.get("id") == silence_id and item.get("status", {}).get("state") == "active"
                    for item in api(base, "GET", "/api/v2/silences")):
                cleanup_errors.append("test silence remained active after cleanup")
        except Exception as error:
            cleanup_errors.append("verify Alertmanager cleanup: " + type(error).__name__)
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)
        if cleanup_errors:
            raise RuntimeError("Alertmanager E2E cleanup failed: " + "; ".join(cleanup_errors))


def proxy_ready(port):
    try:
        with urlopen("http://127.0.0.1:" + str(port) + "/version", timeout=1):
            return True
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


if __name__ == "__main__":
    main()
