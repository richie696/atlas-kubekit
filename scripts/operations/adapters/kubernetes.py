# SPDX-License-Identifier: MIT
"""A bounded kubectl adapter. Never publishes raw stdout/stderr on failure."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from ..models import OperationError
from .ssh import arguments

KINDS = frozenset(
    (
        "namespace",
        "configmap",
        "node",
        "pod",
        "pdb",
        "deployment",
        "daemonset",
        "replicaset",
        "statefulset",
        "service",
        "endpointslice",
        "persistentvolume",
        "persistentvolumeclaim",
        "storageclass",
        "event",
        "volumeattachment",
        "replicas.longhorn.io",
    )
)


def resource_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value
    ):
        raise OperationError("ops.change.invalid_parameters")
    return value


class Kubernetes:
    def __init__(self, kubeconfig, control_target=None, identity=None, ssh_config=None):
        self.kubeconfig = str(kubeconfig)
        self.control_target = control_target
        self.identity = identity
        self.ssh_config = ssh_config
        if not self.kubeconfig.startswith("/") or any(ord(c) < 32 for c in self.kubeconfig):
            raise OperationError("ops.change.invalid_parameters")
        if control_target and not re.fullmatch(
            r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", control_target
        ):
            raise OperationError("ops.error.target")

    def call(self, args, value=None, timeout=45, json_output=True, allow_failure=False):
        request_timeout = max(15, min(timeout - 5, 600)) if timeout > 45 else 15
        command = [
            "kubectl",
            "--kubeconfig=" + self.kubeconfig,
            "--request-timeout=" + str(request_timeout) + "s",
            *args,
        ]
        if self.control_target:
            ssh = arguments(self.control_target, self.identity, self.ssh_config)
            command = ssh + [self.control_target, shlex.join(["sudo", "-n", *command])]
        payload = json.dumps(value).encode() if value is not None else None
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(
                    command,
                    input=payload,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise OperationError("ops.change.kubernetes") from None
            if result.returncode:
                if allow_failure:
                    return None
                raise OperationError("ops.change.kubernetes")
            if output.tell() > 32 * 1024 * 1024:
                raise OperationError("ops.error.limit")
            output.seek(0)
            raw = output.read().decode("utf-8")
        if not json_output:
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            raise OperationError("ops.change.kubernetes") from None

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind not in KINDS:
            raise OperationError("ops.change.invalid_parameters")
        args = ["get", kind]
        if name:
            args.append(resource_name(name))
        if namespace:
            args += ["-n", resource_name(namespace)]
        elif kind in (
            "pod",
            "pdb",
            "deployment",
            "daemonset",
            "replicaset",
            "statefulset",
            "service",
            "endpointslice",
            "persistentvolumeclaim",
            "event",
        ):
            args += ["--all-namespaces"]
        if optional:
            args += ["--ignore-not-found"]
        args += ["-o", "json"]
        if optional:
            raw = self.call(args, json_output=False)
            if not raw.strip():
                return None
            try:
                return json.loads(raw)
            except ValueError:
                raise OperationError("ops.change.kubernetes") from None
        return self.call(args)

    def create(self, value):
        return self.call(["create", "-f", "-", "-o", "json"], value=value, allow_failure=True)

    def replace(self, value):
        return self.call(["replace", "-f", "-", "-o", "json"], value=value, allow_failure=True)

    def cluster_identity(self):
        return self.get("namespace", "kube-system")["metadata"]["uid"]

    def version(self):
        return self.call(["version", "-o", "json"])["serverVersion"]["gitVersion"]

    def endpoint(self):
        return self.call(
            ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
            json_output=False,
        ).strip()

    def node_pods(self, name):
        return self.call(
            [
                "get",
                "pods",
                "--all-namespaces",
                "--field-selector=spec.nodeName=" + resource_name(name),
                "-o",
                "json",
            ]
        )["items"]

    def delete_node(self, name, uid):
        value = dict(apiVersion="v1", kind="DeleteOptions", preconditions=dict(uid=uid))
        return self.call(
            ["delete", "--raw=/api/v1/nodes/" + resource_name(name), "-f", "-"], value=value
        )

    def evict_pod(self, namespace, name, uid):
        value=dict(apiVersion='policy/v1',kind='Eviction',metadata=dict(name=resource_name(name),
            namespace=resource_name(namespace)),deleteOptions=dict(preconditions=dict(uid=uid)))
        return self.call(['create','--raw=/api/v1/namespaces/'+resource_name(namespace)+'/pods/'+
                          resource_name(name)+'/eviction','-f','-'],value=value)
