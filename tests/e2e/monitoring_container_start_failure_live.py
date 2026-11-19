#!/usr/bin/env python3
"""Verify the real managed alert for a container blocked by invalid configuration."""
import argparse
import json
import time
import uuid
from pathlib import Path

from monitoring_workload_failure_live import (
    BUSYBOX,
    command,
    delete_namespace,
    matching_alerts,
    resource,
    verify_lab,
    wait_for,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e":
        parser.error("explicit confirmation atlas-monitor-e2e is required")

    client = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context]
    verify_lab(client, args.kubeconfig, args.context, args.prometheus_url)
    namespace = "atlas-start-failure-e2e-" + uuid.uuid4().hex[:8]
    pod = "missing-secret-probe"
    created = False
    failed = False
    try:
        command(client + ["create", "namespace", namespace])
        created = True
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod, "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "containers": [{"name": "probe", "image": BUSYBOX,
                                     "imagePullPolicy": "IfNotPresent",
                                     "command": ["/bin/sh", "-c", "sleep 600"],
                                     "env": [{"name": "REQUIRED_VALUE",
                                              "valueFrom": {"secretKeyRef": {
                                                  "name": "atlas-e2e-secret-that-does-not-exist",
                                                  "key": "value"}}}]}]},
        }
        command(client + ["apply", "-f", "-"], input_text=json.dumps(manifest))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            pod_data = resource(client, "-n", namespace, "get", "pod", pod)
            statuses = pod_data.get("status", {}).get("containerStatuses", [])
            waiting_reason = statuses[0].get("state", {}).get("waiting", {}).get("reason") if statuses else None
            if waiting_reason == "CreateContainerConfigError":
                break
            time.sleep(3)
        else:
            raise RuntimeError("Kubernetes did not report CreateContainerConfigError for the test pod")
        print("PASS: Kubernetes reported the actual CreateContainerConfigError reason.", flush=True)

        wait_for(client, args.prometheus_url, namespace, pod,
                 {"AtlasPodContainerStartFailure"},
                 lambda items: any(item.get("state") == "firing" and
                                    item.get("labels", {}).get("reason") == "CreateContainerConfigError"
                                    for item in items),
                 240, "real configuration failure fired AtlasPodContainerStartFailure with correct labels")

        command(client + ["-n", namespace, "delete", "pod", pod, "--wait=true", "--timeout=30s"],
                timeout=40)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if not matching_alerts(args.prometheus_url, namespace, pod, {"AtlasPodContainerStartFailure"}):
                print("PASS: deleting the invalid workload cleared the start-failure alert.", flush=True)
                break
            time.sleep(5)
        else:
            raise RuntimeError("start-failure alert did not resolve after deleting the test pod")
    except BaseException:
        failed = True
        raise
    finally:
        if created:
            error = delete_namespace(client, namespace)
            if error:
                if failed:
                    print("CLEANUP_FAILED: " + error)
                else:
                    raise RuntimeError(error)
            elif matching_alerts(args.prometheus_url, namespace, pod, {"AtlasPodContainerStartFailure"}):
                message = "start-failure alert remained after namespace cleanup"
                if failed:
                    print("CLEANUP_FAILED: " + message)
                else:
                    raise RuntimeError(message)
            else:
                print("PASS: temporary namespace and start-failure alert were cleaned.", flush=True)


if __name__ == "__main__":
    main()
