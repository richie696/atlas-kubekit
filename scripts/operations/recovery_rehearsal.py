# SPDX-License-Identifier: MIT
"""Serve an isolated restore over loopback mTLS, verify its identity, then stop it."""
import base64
import json
from pathlib import Path
import re
import socket
import subprocess
import time

from . import backup, etcd_tools
from .host_actions import common
from .models import OperationError
from .recovery import ISOLATED_PEER
from .reporting import atomic_json, private_directory

CLIENT = "https://127.0.0.1:12379"


def reserve_check(ports=(12379, 12380)):
    # No existing listener is stopped or displaced. A later bind race fails closed.
    sockets = []
    try:
        for port in ports:
            item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(item)
            item.bind(("127.0.0.1", port))
    except OSError:
        raise OperationError("ops.restore.ports") from None
    finally:
        for item in sockets:
            item.close()


def command(target, executable):
    pki = target / "configuration/etc/kubernetes/pki/etcd"
    for name in (
        "ca.crt",
        "server.crt",
        "server.key",
        "peer.crt",
        "peer.key",
        "healthcheck-client.crt",
        "healthcheck-client.key",
    ):
        common.read_owned(pki / name)
    return [
        executable,
        "--name=isolated-recovery",
        "--data-dir=" + str(target / "etcd"),
        "--listen-client-urls=" + CLIENT,
        "--advertise-client-urls=" + CLIENT,
        "--listen-peer-urls=" + ISOLATED_PEER,
        "--initial-advertise-peer-urls=" + ISOLATED_PEER,
        "--initial-cluster=isolated-recovery=" + ISOLATED_PEER,
        "--cert-file=" + str(pki / "server.crt"),
        "--key-file=" + str(pki / "server.key"),
        "--trusted-ca-file=" + str(pki / "ca.crt"),
        "--client-cert-auth=true",
        "--peer-cert-file=" + str(pki / "peer.crt"),
        "--peer-key-file=" + str(pki / "peer.key"),
        "--peer-trusted-ca-file=" + str(pki / "ca.crt"),
        "--peer-client-cert-auth=true",
        "--log-level=warn",
    ]


def client(target, executable):
    pki = target / "configuration/etc/kubernetes/pki/etcd"
    return [
        executable,
        "--endpoints=" + CLIENT,
        "--dial-timeout=2s",
        "--command-timeout=3s",
        "--cacert=" + str(pki / "ca.crt"),
        "--cert=" + str(pki / "healthcheck-client.crt"),
        "--key=" + str(pki / "healthcheck-client.key"),
    ]


def run(destination, expected_source):
    backup.require_root()
    target = private_directory(Path(destination))
    record = json.loads(common.read_owned(target / "restore.json"))
    if (
        record.get("schemaVersion") != 1
        or record.get("operation") != "isolated-restore"
        or record.get("sourceFingerprint") != expected_source
        or record.get("verification", {}).get("etcdRestored") is not True
    ):
        raise OperationError("ops.restore.rehearsal_scope")
    dependencies = record.get("dependencies", {})
    identity = dependencies.get("clusterIdentity")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", identity):
        raise OperationError("ops.restore.cluster_identity")
    server = etcd_tools.for_backup("etcd", dependencies)
    ctl = etcd_tools.for_backup("etcdctl", dependencies)
    if not server or not ctl:
        raise OperationError("ops.backup.tools")
    log = target / "rehearsal-etcd.log"
    if log.exists() or log.is_symlink():
        raise OperationError("ops.error.path")
    with backup.operation_lock():
        reserve_check()
        args = command(target, server)
        control = client(target, ctl)
        process = None
        succeeded = False
        fd = log.open("xb")
        log.chmod(0o600)
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd)
            deadline = time.monotonic() + 45
            while True:
                if process.poll() is not None:
                    raise OperationError("ops.restore.rehearsal_failed")
                try:
                    common.run([*control, "endpoint", "health"], timeout=5)
                    break
                except OperationError:
                    if time.monotonic() >= deadline:
                        raise OperationError("ops.restore.rehearsal_failed") from None
                    time.sleep(0.5)
            _, raw = common.run(
                [*control, "get", "/registry/namespaces/kube-system", "--write-out=json"], timeout=5
            )
            rows = json.loads(raw).get("kvs", [])
            if len(rows) != 1 or identity.encode() not in base64.b64decode(
                rows[0]["value"], validate=True
            ):
                raise OperationError("ops.restore.cluster_identity")
            if process.poll() is not None:
                raise OperationError("ops.restore.rehearsal_failed")
            succeeded = True
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            fd.close()
            atomic_json(
                target / "rehearsal.json",
                dict(
                    schemaVersion=1,
                    state="succeeded" if succeeded else "failed",
                    clusterIdentity=identity,
                    stopped=process is None or process.poll() is not None,
                ),
            )
        record["verification"].update(
            etcdServing=True, etcdStopped=True, clusterIdentityVerified=True
        )
        atomic_json(target / "restore.json", record)
    return record
