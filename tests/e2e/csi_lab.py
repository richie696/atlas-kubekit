"""Install a pinned, reduced upstream sample CSI driver into a disposable lab.

The hostpath sample is single-node and non-production; never use this helper
against an existing or production cluster. The caller owns cluster cleanup.
"""
import json
from pathlib import Path
import subprocess
import yaml
from tests.e2e.monitoring_kubernetes import command

COMMIT = "8b88d04ec388312df1b910bf1cb48926c63c2660"
IMAGES = {
    "hostpath": "registry.k8s.io/sig-storage/hostpathplugin:v1.17.0",
    "node-driver-registrar": "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.14.0",
    "csi-provisioner": "registry.k8s.io/sig-storage/csi-provisioner:v5.3.0",
    "liveness-probe": "registry.k8s.io/sig-storage/livenessprobe:v2.16.0",
}
ATTACHER_IMAGE = "registry.k8s.io/sig-storage/csi-attacher:v4.13.0"


def install(kubectl, source, node, *, enable_attach=False):
    if type(enable_attach) is not bool:
        raise ValueError("CSI attach capability flag must be boolean")
    source = Path(source).resolve(strict=True)
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "v1.17.0^{commit}"],
                            check=True, capture_output=True, text=True, timeout=15)
    if result.stdout.strip() != COMMIT:
        raise ValueError("unexpected CSI source revision")
    raw = subprocess.run(["git", "-C", str(source), "show",
        COMMIT + ":deploy/kubernetes-1.30/hostpath/csi-hostpath-plugin.yaml"],
        check=True, capture_output=True, text=True, timeout=15).stdout
    documents = list(yaml.safe_load_all(raw))
    objects = [obj for obj in documents if isinstance(obj, dict) and obj.get("kind") in ("ServiceAccount", "StatefulSet")]
    if len(objects) != 2:
        raise ValueError("unexpected CSI sample manifest")
    statefulset = next(obj for obj in objects if obj["kind"] == "StatefulSet")
    spec = statefulset["spec"]["template"]["spec"]
    spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    if node.endswith("-control-plane"):
        spec["tolerations"] = [{"operator": "Exists"}]
    spec["containers"] = [container for container in spec["containers"] if container["name"] in IMAGES]
    if {container["name"] for container in spec["containers"]} != set(IMAGES):
        raise ValueError("unexpected CSI sample containers")
    for container in spec["containers"]:
        container["image"] = IMAGES[container["name"]]
        # Images are archived into the disposable kind nodes before install;
        # do not let the upstream sample's Always policy require registry access.
        container["imagePullPolicy"] = "IfNotPresent"
    if enable_attach:
        hostpath = next(container for container in spec["containers"] if container["name"] == "hostpath")
        hostpath["args"].append("--enable-attach")
        spec["containers"].append({
            "name": "external-attacher",
            "image": ATTACHER_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "args": ["--v=5", "--csi-address=/csi/csi.sock", "--leader-election",
                     "--leader-election-namespace=default"],
            "volumeMounts": [{"name": "socket-dir", "mountPath": "/csi"}],
        })
    # The disposable test cluster has no other tenants or workloads. This broad
    # RBAC is intentionally limited to this sample test and is not product setup.
    objects.extend([{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
        "metadata": {"name": "atlas-csi-e2e-admin"},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [{"kind": "ServiceAccount", "name": "csi-hostpathplugin-sa", "namespace": "default"}]},
        {"apiVersion": "storage.k8s.io/v1", "kind": "CSIDriver", "metadata": {"name": "hostpath.csi.k8s.io"},
         "spec": {"attachRequired": enable_attach, "podInfoOnMount": True, "fsGroupPolicy": "File"}},
        {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "atlas-csi-e2e"},
         "provisioner": "hostpath.csi.k8s.io", "reclaimPolicy": "Delete", "volumeBindingMode": "WaitForFirstConsumer"}])
    command(kubectl + ["apply", "-f", "-"], data={"apiVersion": "v1", "kind": "List", "items": objects})
    command(kubectl + ["-n", "default", "rollout", "status", "statefulset/csi-hostpathplugin", "--timeout=120s"], timeout=130)
