#!/usr/bin/env python3
"""Exercise real Alertmanager webhook delivery against a disposable receiver."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.e2e.monitoring_prometheus import cleanup_owned, docker

RECEIVER = '''import sys
sys.path.insert(0, "/work")
from scripts.operations.monitoring.channels import dispatch, relay
attempts=0
def vendor(endpoint,text,opener):
 global attempts
 firing = "[告警触发]" in text
 resolved = "[告警恢复]" in text
 required = ("告警规则: 节点未就绪或监控数据缺失", "风险编号: R-NODE-01", "摘要:", "处理建议:", "开始时间:")
 node_ok = any(field in text for field in ("节点: 临时测试节点", "节点: cp1"))
 summary_ok = any(summary in text for summary in (
    "节点临时测试节点未就绪或就绪指标缺失", "节点临时测试节点已恢复就绪", "节点 cp1 未就绪或监控数据缺失"))
 details_ok = any(detail in text for detail in (
    "请检查该节点的 kubelet 心跳和网络连通性；监控数据缺失时先核对采集目标。",
    "请确认该节点 Ready 状态和最新监控样本已恢复。", "检查节点和监控采集状态。"))
 test_value_ok = ("临时测试节点" not in text or
                  (("观测值: Ready=1（测试注入）" in text) if resolved else
                   ("观测值: Ready=0（测试注入）" in text)))
 if (not all(field in text for field in required) or not node_ok or not summary_ok or not details_ok or
      not test_value_ok or (not firing and not resolved)):
  print("INVALID localized alert content",flush=True); return "rejected"
 if firing and attempts == 0:
  attempts += 1; print("RETRY firing",flush=True); return "unknown"
 print("RECEIVED " + ("resolved" if resolved else "firing"),flush=True)
 return "accepted"
dispatch.wecom.deliver=vendor
routes={"version":1,"routes":[{"receiver":"team-a","kind":"wecom","language":"zh_CN",
        "endpoint_env":"ATLAS_RELAY_WECOM_ENDPOINT"}]}
deliver=relay.route_dispatch(routes,{"ATLAS_RELAY_WECOM_ENDPOINT":"unused"})
relay.make_server("127.0.0.1",8000,"a"*32,deliver).serve_forever()
'''
CONFIG = '''route:
  receiver: team-a
  group_by: [alertname]
  group_wait: 1s
  group_interval: 2s
  repeat_interval: 30s
receivers:
  - name: team-a
    webhook_configs:
      - url: http://127.0.0.1:8000/v1/alert
        send_resolved: true
        http_config:
          authorization:
            credentials: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
'''


def wait_log(container, expected):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        output = docker("logs", container).stdout
        if "INVALID" in output:
            raise RuntimeError("receiver rejected Alertmanager webhook")
        if "RECEIVED " + expected in output:
            print("Alertmanager delivered " + expected)
            return
        time.sleep(1)
    raise RuntimeError("Alertmanager did not deliver " + expected)


def post(url, start, end):
    resolved = end <= datetime.now(timezone.utc)
    summary = "节点临时测试节点已恢复就绪" if resolved else "节点临时测试节点未就绪或就绪指标缺失"
    description = ("请确认该节点 Ready 状态和最新监控样本已恢复。" if resolved else
                   "请检查该节点的 kubelet 心跳和网络连通性；监控数据缺失时先核对采集目标。")
    observed_value = "Ready=1（测试注入）" if resolved else "Ready=0（测试注入）"
    body = json.dumps([{"labels": {"alertname": "AtlasNodeReadinessOrTelemetryMissing", "node": "临时测试节点", "severity": "warning",
                                   "risk_id": "R-NODE-01"},
                       "annotations": {"summary": summary, "description": description,
                                       "observed_value": observed_value},
                        "startsAt": start.isoformat(), "endsAt": end.isoformat()}]).encode()
    request = Request(url + "/api/v2/alerts", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Alertmanager API rejected synthetic alert")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    network = "atlas-am-e2e-" + suffix
    receiver, manager = "atlas-am-receiver-" + suffix, "atlas-am-manager-" + suffix
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-am-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "receiver.py").write_text(RECEIVER)
            (root / "alertmanager.yml").write_text(CONFIG)
            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)
            docker("run", "-d", "--pull=never", "--name", manager,
                   "--label", "atlas-kubekit.e2e=" + suffix, "--network", network,
                   "-p", "127.0.0.1::9093", "-v", str(root / "alertmanager.yml") + ":/etc/alertmanager/alertmanager.yml:ro",
                   "prom/alertmanager:v0.28.1", "--config.file=/etc/alertmanager/alertmanager.yml",
                   "--cluster.listen-address=")
            docker("run", "-d", "--pull=never", "--name", receiver,
                   "--label", "atlas-kubekit.e2e=" + suffix, "--network", "container:" + manager,
                   "-v", str(root / "receiver.py") + ":/tmp/receiver.py:ro",
                   "-v", str(ROOT) + ":/work:ro", "python:3.13-slim", "python", "-u", "/tmp/receiver.py")
            port = docker("port", manager, "9093/tcp").stdout.strip().rsplit(":", 1)[1]
            url = "http://127.0.0.1:" + port
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(url + "/-/ready", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Alertmanager failed to start")
                time.sleep(1)
            start = datetime.now(timezone.utc) - timedelta(seconds=5)
            post(url, start, datetime.now(timezone.utc) + timedelta(minutes=5))
            wait_log(receiver, "firing")
            if "RETRY firing" not in docker("logs", receiver).stdout:
                raise RuntimeError("Alertmanager retry was not exercised")
            print("Alertmanager retried a temporary receiver failure")
            post(url, start, datetime.now(timezone.utc) - timedelta(seconds=1))
            wait_log(receiver, "resolved")
    finally:
        cleanup_owned(suffix, network, (receiver, manager))
        print("Owned temporary containers and network cleaned up.")


if __name__ == "__main__":
    main()
