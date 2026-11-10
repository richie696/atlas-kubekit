# SPDX-License-Identifier: MIT
"""Read-only node facts; self-contained for the SSH adapter's fixed payload."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess


def command(args):
    if not shutil.which(args[0]):
        return None
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return p if len(p.stdout) < 65536 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def configuration_block(text, pattern):
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise ValueError()
    start = matches[0].end()
    depth = 1
    quote = None
    comment = False
    escape = False
    for index in range(start, len(text)):
        value = text[index]
        if comment:
            if value == "\n":
                comment = False
            continue
        if quote:
            if escape:
                escape = False
            elif value == "\\":
                escape = True
            elif value == quote:
                quote = None
            continue
        if value in ('"', "'"):
            quote = value
        elif value in ("#", "!"):
            comment = True
        elif value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise ValueError()


def lb_configuration(keep, hap):
    """Return only deployment metadata; authentication values never escape."""
    instance = configuration_block(keep, r"^[ \t]*vrrp_instance[ \t]+VI_KUBE_APISERVER[ \t]*\{")
    vip = configuration_block(instance, r"^[ \t]*virtual_ipaddress[ \t]*\{")
    vips = sorted(
        set(
            str(ipaddress.IPv4Address(value))
            for value in re.findall(r"^[ \t]*([0-9.]+)(?:/[0-9]+)?(?:[ \t]|$)", vip, re.M)
        )
    )
    interfaces = re.findall(r"^[ \t]*interface[ \t]+([A-Za-z0-9_.:-]+)[ \t]*$", instance, re.M)
    ids = re.findall(r"^[ \t]*virtual_router_id[ \t]+([0-9]+)[ \t]*$", instance, re.M)
    if not vips or len(interfaces) != 1 or len(ids) != 1:
        raise ValueError()

    def section(kind, name):
        pattern = (
            r"^[ \t]*"
            + kind
            + r"[ \t]+"
            + name
            + r"[ \t]*\n(.*?)(?=^[ \t]*(?:frontend|backend|listen|global|defaults)\b|\Z)"
        )
        values = re.findall(pattern, hap, re.M | re.S)
        if len(values) != 1:
            raise ValueError()
        return values[0]

    front = section("frontend", "kube-apiserver")
    binds = re.findall(r"^[ \t]*bind[ \t]+([^\n]+)", front, re.M)
    ports = re.findall(r"^[ \t]*bind[ \t]+(?:\*|0\.0\.0\.0):([0-9]+)[ \t]*$", front, re.M)
    routes = re.findall(r"^[ \t]*default_backend[ \t]+([^\n]+)", front, re.M)
    back = section("backend", "kube-apiserver-backend")
    servers = re.findall(
        r"^[ \t]*server[ \t]+(cp[1-9][0-9]*)[ \t]+([0-9.]+):([0-9]+)[ \t]+", back, re.M
    )
    server_lines = re.findall(r"^[ \t]*server[ \t]+([^\n]+)", back, re.M)
    if (
        len(binds) != 1
        or len(ports) != 1
        or [r.strip() for r in routes] != ["kube-apiserver-backend"]
        or not servers
        or len(server_lines) != len(servers)
        or len({s[0] for s in servers}) != len(servers)
    ):
        raise ValueError()
    stats = section("frontend", "stats")
    stats_binds = re.findall(r"^[ \t]*bind[ \t]+([^\n]+)", stats, re.M)
    bound = re.findall(r"^[ \t]*bind[ \t]+(\S+):7000[ \t]*$", stats, re.M)
    allowed = re.findall(
        r"^[ \t]*acl[ \t]+stats_source_allowed[ \t]+src[ \t]+([^\n]+)", stats, re.M
    )
    policy = "unverified"
    cidrs = []
    if len(bound) == 1 and len(stats_binds) == 1:
        if bound[0] in ("127.0.0.1", "[::1]"):
            policy = "loopback"
        elif len(allowed) == 1 and re.search(
            r"^[ \t]*http-request[ \t]+deny[ \t]+if[ \t]+!stats_source_allowed[ \t]*$", stats, re.M
        ):
            cidrs = [str(ipaddress.IPv4Network(v, strict=False)) for v in allowed[0].split()]
            policy = "cidr"
        elif not allowed:
            policy = "unrestricted"
    return dict(
        state="ok",
        vips=vips,
        interface=interfaces[0],
        vrrpID=int(ids[0]),
        apiPort=int(ports[0]),
        backends=[
            dict(name=n, address=str(ipaddress.IPv4Address(a)), port=int(p)) for n, a, p in servers
        ],
        statsPolicy=policy,
        statsCIDRs=cidrs,
    )


def deployment_values(text):
    """Parse selected scalar assignments, never source/eval a shell file."""
    allowed = {
        "PROFILE",
        "VIP",
        "APP_INGRESS_VIP",
        "API_PORT",
        "VRRP_ID",
        "POD_CIDR",
        "SVC_CIDR",
        "LB_STATS_ALLOWED_CIDR",
        "LOCAL_REGISTRY_HOST",
        "SSH_USER",
    }
    values = {}
    for row in text.splitlines():
        match = re.fullmatch(r"([A-Z_]+)=(.*)", row)
        if match and match[1] in allowed:
            if match[1] in values:
                raise ValueError()
            parsed = shlex.split(match[2])
            if len(parsed) != 1 or len(parsed[0]) > 256 or any(ord(c) < 32 for c in parsed[0]):
                raise ValueError()
            values[match[1]] = parsed[0]
    return dict(state="ok", values=values)


def identity_facts():
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        machine = ""
    name = socket.gethostname().split(".")[0]
    return dict(
        hostname=name[:253],
        identityState="ok" if re.fullmatch(r"[0-9a-fA-F]{32}", machine) else "unverified",
        identityFingerprint=hashlib.sha256((name + "\n" + machine).encode()).hexdigest(),
        machineIdFingerprint=hashlib.sha256(machine.encode()).hexdigest(),
    )


def registry_configurations():
    result=[]
    try:
        base=Path('/etc/containerd/certs.d')
        files=list(base.glob('*/hosts.toml'))
        if len(files)>256:return dict(state='unverified')
        for file in files:
            if any(path.is_symlink() for path in (file,*file.parents)) or file.stat().st_size>64*1024:return dict(state='unverified')
            text=file.read_text()
            if '# Managed by Atlas KubeKit: HTTPS registry trust.' not in text:continue
            hosts=re.findall(r'(?m)^server = "https://([A-Za-z0-9.-]+:[0-9]{1,5})"$',text)
            ca=re.findall(r'(?m)^\s*ca = "([^"]+)"$',text)
            if len(hosts)!=1 or len(ca)>1:return dict(state='unverified')
            fingerprint=''
            if ca:
                path=Path(ca[0]);match=re.fullmatch(r'atlas-ca-([a-f0-9]{64})\.crt',path.name)
                if (path.parent!=file.parent or not match or path.is_symlink()
                        or path.stat().st_size>64*1024):return dict(state='unverified')
                fingerprint=hashlib.sha256(path.read_bytes()).hexdigest()
                if fingerprint!=match[1]:return dict(state='unverified')
            result.append(dict(authority=hosts[0],caFingerprint=fingerprint))
        return dict(state='ok',entries=sorted(result,key=lambda item:item['authority']))
    except (OSError,ValueError):return dict(state='unverified')


def collect():
    identity = identity_facts()
    name = identity["hostname"]
    role = (
        "cp"
        if re.fullmatch(r"cp[1-9][0-9]*", name)
        else (
            "lb"
            if re.fullmatch(r"lb[1-9][0-9]*", name)
            else "worker" if re.fullmatch(r"w[1-9][0-9]*", name) else "unknown"
        )
    )
    facts = dict(
        schemaVersion=1,
        hostname=name[:253],
        role=role,
        privileged=os.geteuid() == 0,
        identityFingerprint=identity["identityFingerprint"],
        identityState=identity["identityState"],
        bootstrap=dict(state="unverified"),
        pairing=dict(state="unverified"),
        ssh=dict(state="unverified"),
        stats=dict(state="unverified"),
    )
    try:
        p = Path("/etc/sudoers.d/90-k8s-bootstrap")
        try:
            text = p.read_text()
        except FileNotFoundError:
            text = ""
        facts["bootstrap"] = dict(state="ok", broad=bool(re.search(r"NOPASSWD:\s*ALL\b", text)))
    except OSError:
        pass
    pairing = command(
        [
            "systemctl",
            "show",
            "k8s-enroll.service",
            "--property=ActiveState",
            "--property=LoadState",
        ]
    )
    if pairing is not None and pairing.returncode == 0:
        values = dict(line.split("=", 1) for line in pairing.stdout.splitlines() if "=" in line)
        if values.get("ActiveState") in (
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
        ):
            facts["pairing"] = dict(
                state="ok", active=values["ActiveState"] not in ("inactive", "failed")
            )
    sshd = command(["/usr/sbin/sshd", "-T"])
    if sshd is not None and sshd.returncode == 0:
        wanted = {"passwordauthentication", "kbdinteractiveauthentication", "permitrootlogin"}
        settings = {
            p[0]: p[1]
            for line in sshd.stdout.splitlines()
            if len(p := line.split()) == 2 and p[0] in wanted
        }
        facts["ssh"] = dict(state="ok", settings=settings)
    sockets = command(["ss", "-H", "-lnt", "sport = :7000"])
    if sockets is not None and sockets.returncode == 0:
        addresses = [p[3] for row in sockets.stdout.splitlines() if len(p := row.split()) >= 4]
        facts["stats"] = dict(state="ok", listeners=sorted(set(addresses)))
    facts["services"] = {}
    for service in ("containerd", "kubelet", "haproxy", "keepalived"):
        status = command(["systemctl", "is-active", service])
        value = status.stdout.strip() if status is not None else ""
        facts["services"][service] = (
            value
            if value in ("active", "inactive", "failed", "activating", "deactivating")
            else None
        )
    facts["addresses"] = dict(state="unverified")
    addresses = command(["ip", "-j", "-4", "addr", "show"])
    if addresses is not None and addresses.returncode == 0:
        try:
            links = json.loads(addresses.stdout)
            values = [
                dict(interface=row["ifname"], address=str(ipaddress.IPv4Address(a["local"])))
                for row in links
                for a in row.get("addr_info", [])
                if a.get("family") == "inet"
            ]
            facts["addresses"] = dict(state="ok", values=values)
        except (ValueError, KeyError, TypeError):
            pass
    facts["deployment"] = dict(state="unverified")
    try:
        facts["deployment"] = deployment_values(
            Path("/var/lib/k8s-deploy/cluster.conf").read_text()
        )
    except (OSError, ValueError):
        pass
    facts["lb"] = dict(state="unverified")
    if role == "lb":
        try:
            facts["lb"] = lb_configuration(
                Path("/etc/keepalived/keepalived.conf").read_text(),
                Path("/etc/haproxy/haproxy.cfg").read_text(),
            )
        except (OSError, ValueError):
            pass
    facts["versions"] = {}
    if role in ("cp", "worker"):
        version = command(["kubelet", "--version"])
        if version is not None and version.returncode == 0:
            match = re.fullmatch(r"Kubernetes (v[0-9]+\.[0-9]+\.[0-9]+)", version.stdout.strip())
            if match:
                facts["versions"]["kubernetes"] = match[1]
    facts['registry']=registry_configurations()
    facts["network"] = dict(state="unverified")
    if role == "cp":
        prefix = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "--request-timeout=5s"]
        image = command(
            prefix
            + [
                "-n",
                "kube-system",
                "get",
                "ds",
                "cilium",
                "-o",
                'jsonpath={.spec.template.spec.containers[?(@.name=="cilium-agent")].image}',
            ]
        )
        if image is not None and image.returncode == 0:
            match = re.search(
                r":v?([0-9]+\.[0-9]+\.[0-9]+)(?:@sha256:[0-9a-f]+)?$", image.stdout.strip()
            )
            if match:
                facts["versions"]["cilium"] = match[1]
        network = command(
            prefix
            + [
                "-n",
                "kube-system",
                "get",
                "configmap",
                "kubeadm-config",
                "-o",
                "jsonpath={.data.ClusterConfiguration}",
            ]
        )
        if network is not None and network.returncode == 0:
            try:
                blocks = re.findall(
                    r"^networking:[ \t]*\n((?:[ \t]+[^\n]*(?:\n|$))*)", network.stdout, re.M
                )
                if len(blocks) != 1:
                    raise ValueError()
                subnets = {}
                for field in ("podSubnet", "serviceSubnet"):
                    found = re.findall(r"^[ \t]+" + field + r":[ \t]*(\S+)[ \t]*$", blocks[0], re.M)
                    if len(found) != 1:
                        raise ValueError()
                    subnets[field] = str(ipaddress.IPv4Network(found[0].strip("\"'"), strict=True))
                facts["network"] = dict(state="ok", values=subnets)
            except ValueError:
                pass
    return facts


if __name__ == "__main__":
    print(json.dumps(collect()))
