# SPDX-License-Identifier: MIT
"""A bounded kubectl adapter. Never publishes raw stdout/stderr on failure."""
import json
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
        "job",
        "cronjob",
        "service",
        "serviceaccount",
        "ingress",
        "endpoints",
        "endpointslice",
        "persistentvolume",
        "persistentvolumeclaim",
        "storageclass",
        "event",
        "volumeattachment",
        "replicas.longhorn.io",
        "prometheuses.monitoring.coreos.com",
        "prometheusagents.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
        "podmonitors.monitoring.coreos.com",
        "servicemonitors.monitoring.coreos.com",
        "probes.monitoring.coreos.com",
        "scrapeconfigs.monitoring.coreos.com",
        "thanosrulers.monitoring.coreos.com",
        "networkpolicy",
        "alertmanagers.monitoring.coreos.com",
        "alertmanagerconfigs.monitoring.coreos.com",
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
            "job",
            "cronjob",
            "service",
            "endpoints",
            "endpointslice",
            "persistentvolumeclaim",
            "event",
            "prometheusrules.monitoring.coreos.com",
            "podmonitors.monitoring.coreos.com",
            "servicemonitors.monitoring.coreos.com",
            "alertmanagerconfigs.monitoring.coreos.com",
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
        self._guard_center_resource(value)
        return self.call(["create", "-f", "-", "-o", "json"], value=value, allow_failure=True)

    def _guard_center_resource(self, value):
        """Do not publish central rule/routing resources into a push-only Agent cluster."""
        if not isinstance(value, dict) or value.get("kind") not in ("PrometheusRule", "AlertmanagerConfig"):
            return
        marker = self.get("configmap", "atlas-monitoring-topology", "monitoring", optional=True)
        topology = marker.get("data", {}).get("mode") if isinstance(marker, dict) else None
        if topology == "agent":
            raise OperationError("ops.monitoring.center_resources_require_center")

    def secret_metadata(self, namespace, name, key):
        """Return identity and key presence; kubectl itself reads the Secret."""
        resource_name(namespace)
        resource_name(name)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", key):
            raise OperationError("ops.change.invalid_parameters")
        template = '{{printf "%s\\t%s\\t" .metadata.uid .metadata.resourceVersion}}{{if index .data "' + key + '"}}present{{else}}missing{{end}}'
        output = self.call(['get', 'secret', name, '-n', namespace, '-o', 'go-template=' + template], json_output=False)
        parts = output.split('\t')
        if len(parts) != 3 or not parts[0] or not parts[1] or parts[2] != 'present':
            raise OperationError("ops.monitoring.email_secret")
        return {'uid': parts[0], 'resourceVersion': parts[1]}

    def notification_secret_identities(self, namespace):
        """Read identity and ownership labels for setup-owned Secrets, never their data."""
        namespace = resource_name(namespace)
        template = ('{{range .items}}{{printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" '
                    '.metadata.name .metadata.uid .metadata.resourceVersion '
                    '(index .metadata.labels "atlas-kubekit.io/owner") '
                    '(index .metadata.labels "atlas-kubekit.io/component") '
                    '(index .metadata.labels "atlas-kubekit.io/receiver")}}{{end}}')
        raw = self.call(["get", "secrets", "-n", namespace,
                         "-l", "atlas-kubekit.io/owner=monitoring-notification-setup",
                         "-o", "go-template=" + template], json_output=False)
        result = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 6 or not all(parts[:5]):
                raise OperationError("ops.change.kubernetes")
            if parts[5] in ("<no value>", "%!s(<nil>)"):
                parts[5] = ""
            result.append({"name": resource_name(parts[0]), "uid": parts[1],
                           "resourceVersion": parts[2], "owner": parts[3],
                           "component": parts[4], "receiver": parts[5]})
        return result

    def delete_notification_secret(self, namespace, identity):
        """Delete one owned notification Secret using API-server UID/RV preconditions."""
        namespace = resource_name(namespace)
        required = {"name", "uid", "resourceVersion", "owner", "component", "receiver"}
        if not isinstance(identity, dict) or set(identity) != required:
            raise OperationError("ops.change.invalid_parameters")
        name = resource_name(identity["name"])
        if (identity["owner"] != "monitoring-notification-setup" or
                identity["component"] not in ("route", "relay-auth") or
                (identity["component"] == "route" and
                 (not isinstance(identity["receiver"], str) or not re.fullmatch(
                     r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", identity["receiver"]))) or
                (identity["component"] == "relay-auth" and identity["receiver"] != "") or
                not isinstance(identity["uid"], str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", identity["uid"]) or
                not isinstance(identity["resourceVersion"], str) or not re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,128}", identity["resourceVersion"])):
            raise OperationError("ops.change.invalid_parameters")
        template = ('{{printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s" '
                    '.metadata.name .metadata.uid .metadata.resourceVersion '
                    '(index .metadata.labels "atlas-kubekit.io/owner") '
                    '(index .metadata.labels "atlas-kubekit.io/component") '
                    '(index .metadata.labels "atlas-kubekit.io/receiver")}}')
        raw = self.call(["get", "secret", name, "-n", namespace,
                         "-o", "go-template=" + template], json_output=False)
        parts = raw.split("\t")
        if len(parts) != 6 or parts[5] in ("<no value>", "%!s(<nil>)"):
            parts = parts[:5] + ([""] if len(parts) == 6 else [])
        current = ({"name": parts[0], "uid": parts[1], "resourceVersion": parts[2],
                    "owner": parts[3], "component": parts[4], "receiver": parts[5]}
                   if len(parts) == 6 else None)
        if current != identity:
            raise OperationError("ops.change.stale")
        value = {"apiVersion": "v1", "kind": "DeleteOptions",
                 "preconditions": {"uid": identity["uid"], "resourceVersion": identity["resourceVersion"]}}
        path = "/api/v1/namespaces/" + namespace + "/secrets/" + name
        return self.call(["delete", "--raw=" + path, "-f", "-"], value=value)

    def replace(self, value):
        self._guard_center_resource(value)
        return self.call(["replace", "-f", "-", "-o", "json"], value=value, allow_failure=True)

    def cluster_identity(self):
        return self.get("namespace", "kube-system")["metadata"]["uid"]

    def version(self):
        return self.call(["version", "-o", "json"])["serverVersion"]["gitVersion"]

    def alertmanager_request(self, namespace, service, port, method, endpoint, payload=None):
        """Call only Alertmanager v2 silence endpoints through the Kubernetes Service Proxy."""
        namespace, service = resource_name(namespace), resource_name(service)
        if (method not in ("GET", "POST", "DELETE") or
                not isinstance(port, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,13}[a-z0-9])?", port) or
                endpoint not in ("/api/v2/silences",) and
                not re.fullmatch(r"/api/v2/silence/[a-fA-F0-9-]{8,128}", str(endpoint))):
            raise OperationError("ops.change.invalid_parameters")
        if (method == "GET" and endpoint != "/api/v2/silences" or
                method == "POST" and endpoint != "/api/v2/silences" or
                method == "DELETE" and endpoint == "/api/v2/silences" or
                method == "POST" and not isinstance(payload, dict) or
                method != "POST" and payload is not None):
            raise OperationError("ops.change.invalid_parameters")
        proxy_path = ("/api/v1/namespaces/" + namespace + "/services/http:" + service + ":" + port +
                      "/proxy" + endpoint)
        local_port = self._free_port()
        process = None
        remote_pid = None
        remote_pid_file = None
        try:
            if self.control_target:
                remote_port = self._remote_free_port()
                remote_pid_file = "/run/atlas-kubekit-kubectl-proxy-" + uuid.uuid4().hex + ".pid"
                ssh = arguments(self.control_target, self.identity, self.ssh_config)
                remote_proxy = shlex.join(["kubectl", "--kubeconfig=" + self.kubeconfig, "proxy",
                                           "--address=127.0.0.1", "--port=" + str(remote_port)])
                script = ("umask 077; " + remote_proxy + " >/dev/null 2>&1 & proxy_pid=$!; "
                          "printf '%s\\n' \"$proxy_pid\" > " + shlex.quote(remote_pid_file) + "; "
                          "printf '%s\\n' \"$proxy_pid\"; wait \"$proxy_pid\"")
                command = ssh + ["-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                                 self.control_target,
                                 shlex.join(["sudo", "-n", "sh", "-c", script])]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, bufsize=1)
                remote_pid = self._read_remote_pid(process)
            else:
                command = ["kubectl", "--kubeconfig=" + self.kubeconfig, "proxy",
                           "--address=127.0.0.1", "--port=" + str(local_port)]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = "http://127.0.0.1:" + str(local_port)
            self._wait_proxy(process, base)
            request = Request(base + proxy_path,
                              data=json.dumps(payload).encode() if payload is not None else None,
                              headers={"Content-Type": "application/json"}, method=method)
            try:
                with urlopen(request, timeout=15) as response:
                    body = response.read(1024 * 1024)
            except (HTTPError, URLError, OSError, TimeoutError):
                raise OperationError("ops.monitoring.silence_api") from None
            if method == "DELETE":
                return body.decode("utf-8", errors="replace")
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise OperationError("ops.monitoring.silence_api") from None
        except (OSError, subprocess.SubprocessError):
            raise OperationError("ops.monitoring.silence_api") from None
        finally:
            if process is not None:
                if self.control_target and remote_pid is not None:
                    self._stop_remote_proxy(remote_pid, remote_pid_file)
                elif self.control_target and remote_pid_file is not None:
                    self._stop_remote_proxy(None, remote_pid_file)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    @staticmethod
    def _free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def _remote_free_port(self):
        ssh = arguments(self.control_target, self.identity, self.ssh_config)
        script = "python3 -c " + shlex.quote(
            "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); "
            "print(s.getsockname()[1]); s.close()"
        )
        try:
            result = subprocess.run(ssh + [self.control_target, script], check=True, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
            value = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            raise OperationError("ops.monitoring.silence_api") from None
        if not 1024 <= value <= 65535:
            raise OperationError("ops.monitoring.silence_api")
        return value

    def _read_remote_pid(self, process):
        if process.stdout is None:
            raise OperationError("ops.monitoring.silence_api")
        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready:
            raise OperationError("ops.monitoring.silence_api")
        try:
            value = int(process.stdout.readline().strip())
        except (OSError, ValueError):
            raise OperationError("ops.monitoring.silence_api") from None
        if not 2 <= value <= 2**31 - 1:
            raise OperationError("ops.monitoring.silence_api")
        return value

    def _stop_remote_proxy(self, pid, pid_file):
        ssh = arguments(self.control_target, self.identity, self.ssh_config)
        file_literal = shlex.quote(pid_file)
        expected = str(pid) if pid is not None else ""
        cleanup = ("f=" + file_literal + "; "
                   "if [ -f \"$f\" ] && [ ! -L \"$f\" ]; then "
                   "p=$(cat \"$f\"); "
                   "case \"$p\" in ''|*[!0-9]*) exit 1;; esac; "
                   + ("if [ \"$p\" = " + shlex.quote(expected) + " ]; then " if expected else "") +
                   "kill -TERM \"$p\" 2>/dev/null || true; sleep 0.2; "
                   "kill -KILL \"$p\" 2>/dev/null || true; rm -f -- \"$f\"; fi" +
                   ("; fi" if expected else ""))
        command = ssh + [self.control_target, shlex.join(["sudo", "-n", "sh", "-c", cleanup])]
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _wait_proxy(process, base):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OperationError("ops.monitoring.silence_api")
            try:
                with urlopen(base + "/version", timeout=1):
                    return
            except (HTTPError, URLError, OSError, TimeoutError):
                time.sleep(0.1)
        raise OperationError("ops.monitoring.silence_api")

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

    def delete_monitoring_resource(self, kind, namespace, name, uid, resource_version):
        """Delete one supported namespaced monitoring object with API-server CAS fences."""
        paths = {
            "prometheusrule": ("monitoring.coreos.com/v1", "prometheusrules"),
            "servicemonitor": ("monitoring.coreos.com/v1", "servicemonitors"),
            "podmonitor": ("monitoring.coreos.com/v1", "podmonitors"),
            "alertmanagerconfig": ("monitoring.coreos.com/v1alpha1", "alertmanagerconfigs"),
            "networkpolicy": ("networking.k8s.io/v1", "networkpolicies"),
        }
        if (kind not in paths or
                not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", uid) or
                not isinstance(resource_version, str) or
                not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", resource_version)):
            raise OperationError("ops.change.invalid_parameters")
        group_version, plural = paths[kind]
        group, version = group_version.split("/", 1)
        api_root = "/apis/" + group + "/" + version
        path = (api_root + "/namespaces/" + resource_name(namespace) + "/" + plural + "/" +
                resource_name(name))
        value = dict(apiVersion="v1", kind="DeleteOptions",
                     preconditions=dict(uid=uid, resourceVersion=resource_version))
        return self.call(["delete", "--raw=" + path, "-f", "-"], value=value)

    def evict_pod(self, namespace, name, uid):
        value=dict(apiVersion='policy/v1',kind='Eviction',metadata=dict(name=resource_name(name),
            namespace=resource_name(namespace)),deleteOptions=dict(preconditions=dict(uid=uid)))
        return self.call(['create','--raw=/api/v1/namespaces/'+resource_name(namespace)+'/pods/'+
                          resource_name(name)+'/eviction','-f','-'],value=value)
