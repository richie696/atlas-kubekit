# SPDX-License-Identifier: MIT
"""Private Helm preparation and fixed Cilium effect; no arbitrary release or flags."""
import base64
import json
import tempfile
from pathlib import Path

from ..components import cilium_chart, cilium_policy
from ..collector import identity_facts
from ..models import OperationError, fingerprint
from ..reporting import private_directory
from . import common, private_process

HELM = ["helm", "--kubeconfig", "/etc/kubernetes/admin.conf"]
ANNOTATION = cilium_policy.OWNER


def chart(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"version", "chart", "expected", "rolloutOwner"}
        or not {"version", "chart"} <= set(value)
    ):
        raise OperationError("ops.change.invalid_parameters")
    cilium_policy.version(value["version"])
    try:
        raw = base64.b64decode(value["chart"], validate=True)
    except (ValueError, TypeError):
        raise OperationError("ops.component.chart") from None
    return raw, cilium_chart.inspect(raw, value["version"])


def release():
    _, raw = common.run(
        [*HELM, "list", "-n", "kube-system", "--all", "--filter", "^cilium$", "-o", "json"]
    )
    rows = json.loads(raw)
    if len(rows) != 1 or rows[0]["name"] != "cilium":
        raise OperationError("ops.component.release")
    row = rows[0]
    _, raw = common.run([*HELM, "get", "values", "cilium", "-n", "kube-system", "-o", "json"])
    values = json.loads(raw) or {}
    if not isinstance(values, dict):
        raise OperationError("ops.component.release")
    info = {key: str(row[key]) for key in ("revision", "chart", "status")}
    info["valuesSha"] = fingerprint(values)
    info["rolloutOwner"] = values.get("podAnnotations", {}).get(ANNOTATION)
    return info, values


def prepare(directory, value, owner):
    raw, digest = chart(value)
    info, values = release()
    file = Path(directory) / "cilium.tgz"
    settings = Path(directory) / "values.json"
    common.replace(file, raw)
    common.replace(
        settings, json.dumps(cilium_policy.rollout_values(values, owner), sort_keys=True).encode()
    )
    return info, digest, file, settings


def objects(directory, raw):
    manifest = Path(directory) / "rendered.yaml"
    common.replace(manifest, raw.encode())
    _, rows = common.run(
        [
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "create",
            "--dry-run=client",
            "--validate=false",
            "-f",
            str(manifest),
            "-o",
            "json",
        ],
        timeout=120,
    )
    return cilium_policy.documents(rows)


def render_objects(file, settings):
    _, api = common.run(
        ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "version", "-o", "json"]
    )
    kube_version = json.loads(api)["serverVersion"]["gitVersion"]
    _, versions = common.run(["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "api-versions"])
    capabilities = [
        item for version in versions.splitlines() for item in ("--api-versions", version)
    ]
    _, raw = common.run(
        [
            *HELM,
            "template",
            "cilium",
            str(file),
            "-n",
            "kube-system",
            "--values",
            str(settings),
            "--kube-version",
            kube_version,
            "--is-upgrade",
            "--dry-run=server",
            *capabilities,
        ],
        timeout=120,
    )
    return objects(file.parent, raw)


def render(file, settings, target):
    return cilium_policy.rendered(render_objects(file, settings), target)


def observe(value):
    with tempfile.TemporaryDirectory(prefix=".atlas-cilium-preview-") as temporary:
        directory = Path(temporary)
        info, digest, file, settings = prepare(directory, value, "0" * 64)
        policy = render(file, settings, value["version"])
        common.run(
            [
                *HELM,
                "upgrade",
                "cilium",
                str(file),
                "-n",
                "kube-system",
                "--reset-values",
                "--values",
                str(settings),
                "--dry-run=server",
                "--hide-secret",
            ],
            timeout=120,
        )
    return dict(identity=identity_facts(), release=info, chartSha=digest, rendered=policy)


def apply(value, run_id):
    before = observe(value)
    if value.get("expected") != {key: before[key] for key in ("release", "chartSha", "rendered")}:
        raise OperationError("ops.change.stale")
    owner = value.get("rolloutOwner") or run_id
    if (
        before["release"]["status"] == "deployed"
        and before["release"]["chart"] == "cilium-" + value["version"]
        and before["release"]["rolloutOwner"] == owner
    ):
        return dict(alreadyApplied=True, release=before["release"])
    directory = common.journal(run_id, "component-upgrade")
    info, digest, file, settings = prepare(directory, value, owner)
    if info != before["release"]:
        raise OperationError("ops.change.stale")
    common.record(directory, "component-upgrade", "helm-upgrade", chartSha=digest)
    try:
        private_process.run(
            [
                *HELM,
                "upgrade",
                "cilium",
                str(file),
                "-n",
                "kube-system",
                "--reset-values",
                "--values",
                str(settings),
                "--wait",
                "--timeout",
                "10m",
            ],
            directory / "helm.log",
            timeout=630,
        )
        after, _ = release()
        if after["status"] != "deployed" or after["chart"] != "cilium-" + value["version"]:
            raise OperationError("ops.component.release")
        common.record(directory, "component-upgrade", "templates-applied", chartSha=digest)
        return dict(alreadyApplied=False, release=after)
    except BaseException:
        common.record(directory, "component-upgrade", "failed", chartSha=digest)
        raise


def complete(value, run_id):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "chart",
        "rolloutOwner",
        "policies",
        "operatorPolicy",
    }:
        raise OperationError("ops.change.invalid_parameters")
    chart_bytes, digest = chart({key: value[key] for key in ("version", "chart", "rolloutOwner")})
    policies = value["policies"]
    operator = value["operatorPolicy"]
    if (
        not isinstance(policies, dict)
        or "cilium" not in policies
        or set(policies) - {"cilium", "cilium-envoy"}
    ):
        raise OperationError("ops.component.policy")
    policies_restored = True
    for name, pinned in [*policies.items(), ("cilium-operator", operator)]:
        if not isinstance(pinned, dict) or set(pinned) != {"uid", "strategy"}:
            raise OperationError("ops.component.policy")
        cilium_policy.strategy(pinned["strategy"])
        kind = "deployment" if name == "cilium-operator" else "daemonset"
        _, raw = common.run(
            [
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "kube-system",
                "get",
                kind,
                name,
                "-o",
                "json",
            ]
        )
        actual = json.loads(raw)
        if actual["metadata"]["uid"] != pinned["uid"]:
            raise OperationError("ops.change.stale")
        policies_restored = (
            policies_restored
            and actual["spec"].get("strategy" if kind == "deployment" else "updateStrategy")
            == pinned["strategy"]
        )
    info, current = release()
    if (
        info["status"] != "deployed"
        or info["chart"] != "cilium-" + value["version"]
        or info["rolloutOwner"] != value["rolloutOwner"]
    ):
        raise OperationError("ops.component.release")
    if policies_restored and current == cilium_policy.completed_values(current, policies, operator):
        return dict(release=info, policyRestored=True, alreadyCompleted=True)
    # Keep the original effect journal, with a distinct bounded finalization log.
    directory = private_directory(common.JOURNAL_DIRECTORY / run_id / "completion")
    file = directory / "cilium.tgz"
    settings = directory / "values.json"
    common.replace(file, chart_bytes)
    common.replace(
        settings,
        json.dumps(
            cilium_policy.completed_values(current, policies, operator), sort_keys=True
        ).encode(),
    )
    _, stored = common.run([*HELM, "get", "manifest", "cilium", "-n", "kube-system"])
    before = objects(directory, stored)
    after = render_objects(file, settings)
    cilium_policy.policy_only(before, after)
    common.record(directory, "component-complete", "restore-policies", chartSha=digest)
    private_process.run(
        [
            *HELM,
            "upgrade",
            "cilium",
            str(file),
            "-n",
            "kube-system",
            "--reset-values",
            "--values",
            str(settings),
            "--wait",
            "--timeout",
            "10m",
        ],
        directory / "helm.log",
        timeout=630,
    )
    updated, values = release()
    if updated["status"] != "deployed" or values != cilium_policy.completed_values(
        current, policies, operator
    ):
        raise OperationError("ops.component.policy")
    common.record(directory, "component-complete", "completed", chartSha=digest)
    return dict(release=updated, policyRestored=True)
