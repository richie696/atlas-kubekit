#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Owned LB host effects, distributed by the management adapter as fixed code."""
from atlas_host_lock import (
    host_operation,
    reserve_operation,
    release_operation,
    reservation,
    HostLockError,
)
import hashlib
import ipaddress
from pathlib import Path
import csv, datetime, io, json, os, re, shutil, socket, subprocess, sys, tempfile, time
from atlas_lb_files import atomic_copy


def run(args, check=True):
    r = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        timeout=45,
    )
    if check and r.returncode:
        raise RuntimeError(r.stdout.strip() or "command failed: " + str(args))
    return r.stdout.strip(), r.returncode


base = "/etc/atlas-kubekit"
state_file = base + "/lb-manager.json"
hap = "/etc/haproxy/haproxy.cfg"
keep = "/etc/keepalived/keepalived.conf"
TRANSACTION_ROOT = "/var/lib/atlas-kubekit/lb-transactions"
LEGACY_TRANSACTION_ROOT = "/run/atlas-kubekit-lb-manager"


def transaction_path(token):
    current = TRANSACTION_ROOT + "/" + token
    legacy = LEGACY_TRANSACTION_ROOT + "/" + token
    if os.path.exists(current) and os.path.exists(legacy):
        raise RuntimeError("ops.change.stale")
    return legacy if os.path.exists(legacy) else current


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_private(path, optional=False):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError("ops.error.path")
    if optional and not path.exists():
        return None
    info = path.stat()
    if (
        not path.is_file()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or info.st_size > 4 * 1024 * 1024
    ):
        raise RuntimeError("ops.error.path")
    return path.read_bytes()


def state_read():
    raw = read_private(state_file, True)
    return json.loads(raw) if raw is not None else {"vips": [], "routes": []}


def configuration_revision():
    value = hashlib.sha256()
    for path in (hap, keep, state_file):
        raw = read_private(path, True)
        value.update(
            json.dumps(None if raw is None else hashlib.sha256(raw).hexdigest()).encode() + b"\n"
        )
    return value.hexdigest()


def validate_state(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"vips", "routes"}
        or not isinstance(value["vips"], list)
        or not isinstance(value["routes"], list)
    ):
        raise RuntimeError("ops.change.invalid_parameters")
    if len(value["vips"]) > 1000 or len(value["routes"]) > 1000:
        raise RuntimeError("ops.error.limit")

    def ip(raw):
        if not isinstance(raw, str) or str(ipaddress.IPv4Address(raw)) != raw:
            raise RuntimeError("ops.change.invalid_parameters")

    for address in value["vips"]:
        ip(address)
    if len(set(value["vips"])) != len(value["vips"]):
        raise RuntimeError("ops.change.invalid_parameters")
    names = set()
    bindings = set()
    for row in value["routes"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "mode", "listenPort", "bind", "backends", "websocket"}
            or not isinstance(row["name"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", row["name"])
            or row["mode"] not in ("http", "tcp")
            or type(row["listenPort"]) is not int
            or not 1 <= row["listenPort"] <= 65535
            or row["bind"] != "*"
            or type(row["websocket"]) is not bool
            or not isinstance(row["backends"], list)
            or not 1 <= len(row["backends"]) <= 1000
        ):
            raise RuntimeError("ops.change.invalid_parameters")
        binding = row["bind"] + ":" + str(row["listenPort"])
        if row["name"] in names or binding in bindings:
            raise RuntimeError("ops.change.invalid_parameters")
        names.add(row["name"])
        bindings.add(binding)
        for backend in row["backends"]:
            if not isinstance(backend, str):
                raise RuntimeError("ops.change.invalid_parameters")
            host, sep, port = backend.rpartition(":")
            if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise RuntimeError("ops.change.invalid_parameters")
            ip(host)
    return value


def status():
    out = {}
    for svc in ("haproxy", "keepalived"):
        out[svc] = run(["systemctl", "is-active", svc], False)[0] or "unknown"
    out["hostname"] = run(["hostname", "-s"], False)[0]
    out["addresses"] = run(["bash", "-lc", "ip -o -4 addr show | awk '{print $2\" \"$4}'"], False)[
        0
    ]
    out["haproxy_config"] = run(["haproxy", "-c", "-f", hap], False)[1] == 0
    out["keepalived_config"] = run(["keepalived", "-t", "-f", keep], False)[1] == 0
    out["stats"] = []
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2)
        conn.connect("/run/haproxy/admin.sock")
        conn.sendall(b"show stat\n")
        chunks = []
        while True:
            part = conn.recv(65536)
            if not part:
                break
            chunks.append(part)
        conn.close()
        rows = csv.reader(io.StringIO(b"".join(chunks).decode(errors="replace")))
        header = next(rows, [])
        if header:
            header[0] = header[0].lstrip("# ")
        for values in rows:
            row = dict(zip(header, values))
            if row.get("svname") in ("FRONTEND", "BACKEND"):
                out["stats"].append(
                    {k: row.get(k, "") for k in ("pxname", "svname", "status", "scur", "stot")}
                )
    except Exception:
        pass
    out["state"] = state_read()
    out["configurationRevision"] = configuration_revision()
    out["transactionOwner"] = reservation()
    public = read_private("/etc/atlas-kubekit-operations.json", True)
    out["operationsUser"] = json.loads(public)["operationsUser"] if public else None
    print(json.dumps(out))


def span(text, start):
    op = text.find("{", start)
    if op < 0:
        raise RuntimeError("configuration block opening brace not found")
    depth = 0
    quote = None
    esc = False
    for i in range(op, len(text)):
        c = text[i]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return op, i
    raise RuntimeError("configuration block is incomplete")


def replace_managed(text, begin, end, body, before=None):
    marked = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    block = begin + "\n" + body.rstrip() + "\n" + end
    if marked.search(text):
        return marked.sub(lambda _m: block, text, count=1)
    if before is None:
        return text.rstrip() + "\n\n" + block + "\n"
    return (
        text[:before].rstrip()
        + ("\n" if text[:before].strip() else "")
        + block
        + "\n"
        + text[before:]
    )


def create_candidates(data, token):
    with open(hap) as f:
        old_hap = f.read()
    with open(keep) as f:
        old_keep = f.read()
    if data.get("expectedRevision") != configuration_revision():
        raise RuntimeError("ops.change.stale")
    st = validate_state(data["state"])
    vips = st["vips"]
    routes = st["routes"]
    vip_lines = "\n".join("        " + ip for ip in vips)
    start = old_keep.find("vrrp_instance VI_KUBE_APISERVER")
    if start < 0:
        raise RuntimeError(
            "VI_KUBE_APISERVER not found; refusing to replace Keepalived configuration"
        )
    op, cl = span(old_keep, start)
    instance = old_keep[op + 1 : cl]
    vi = instance.find("virtual_ipaddress")
    if vi < 0:
        raise RuntimeError("virtual_ipaddress block not found")
    vop, vcl = span(instance, vi)
    vblock = instance[vop + 1 : vcl]
    begin = "# BEGIN ATLAS KUBEKIT MANAGED VIPS"
    end = "# END ATLAS KUBEKIT MANAGED VIPS"
    old_state = state_read()
    previous = set(old_state.get("vips", []))
    reserved = set(
        re.findall(
            r"(?m)^\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?=/|\s|$)",
            re.sub(re.escape(begin) + r".*?" + re.escape(end), "", old_keep, flags=re.S),
        )
    )
    local_ips = set(
        re.findall(
            r"\binet\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3})/",
            run(["ip", "-o", "-4", "addr", "show"], False)[0],
        )
    )
    conflicts = (set(vips) - previous) & (reserved | local_ips)
    if conflicts:
        raise RuntimeError(
            "requested VIP already appears in Keepalived or on this node: "
            + ", ".join(sorted(conflicts))
        )
    vblock = replace_managed(vblock, begin, end, vip_lines, before=len(vblock.rstrip()))
    instance = instance[: vop + 1] + vblock + instance[vcl:]
    new_keep = old_keep[: op + 1] + instance + old_keep[cl:]
    lines = []
    for r in routes:
        n = r["name"]
        fe = "ak_" + n
        be = "ak_" + n + "_backend"
        mode = r["mode"]
        port = r["listenPort"]
        bind = r["bind"]
        lines += [
            "",
            "# Atlas KubeKit route: " + n,
            "frontend " + fe,
            "    bind " + bind + ":" + str(port),
            "    mode " + mode,
        ]
        if mode == "http":
            lines += ["    option httplog"]
        else:
            lines += ["    option tcplog"]
        lines += [
            "    default_backend " + be,
            "",
            "backend " + be,
            "    mode " + mode,
            "    balance roundrobin",
        ]
        if r.get("websocket"):
            lines += ["    timeout tunnel 1h"]
        if mode == "http":
            lines += ["    option forwardfor"]
        for ix, b in enumerate(r["backends"], 1):
            lines += ["    server " + n + "_s" + str(ix) + " " + b + " check"]
    body = "\n".join(lines).strip()
    new_hap = replace_managed(
        old_hap, "# BEGIN ATLAS KUBEKIT MANAGED ROUTES", "# END ATLAS KUBEKIT MANAGED ROUTES", body
    )
    token_dir = TRANSACTION_ROOT
    os.makedirs(token_dir, mode=0o700, exist_ok=True)
    d = token_dir + "/" + token
    os.makedirs(d, mode=0o700, exist_ok=False)
    with open(d + "/revision", "w") as f:
        f.write(configuration_revision())
    for name, content in (
        ("haproxy.new", new_hap),
        ("keepalived.new", new_keep),
        ("haproxy.old", old_hap),
        ("keepalived.old", old_keep),
    ):
        with open(d + "/" + name, "w") as f:
            f.write(content)
        os.chmod(d + "/" + name, 0o600)
    run(["haproxy", "-c", "-f", d + "/haproxy.new"])
    run(["keepalived", "-t", "-f", d + "/keepalived.new"])
    with open(d + "/state.new", "w") as f:
        json.dump(st, f)
    previous_state = read_private(state_file, True)
    with open(d + "/state.old", "wb") as f:
        f.write(previous_state if previous_state is not None else b"{}")
    if os.path.exists(state_file):
        open(d + "/state.existed", "w").close()
    os.chmod(d + "/state.new", 0o600)
    os.chmod(d + "/state.old", 0o600)
    for path in Path(d).iterdir():
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    sync_directory(d)
    sync_directory(token_dir)
    print(json.dumps({"prepared": True}))


def commit(data):
    token = data["token"]
    d = transaction_path(token)
    if not os.path.isdir(d):
        raise RuntimeError("prepared transaction not found")
    if read_private(d + "/revision").decode() != configuration_revision():
        raise RuntimeError("ops.change.stale")
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = "/var/backups/atlas-kubekit/lb-manager/" + stamp
    os.makedirs(backup, mode=0o700, exist_ok=True)
    os.makedirs(base, mode=0o755, exist_ok=True)
    for name, path in (("haproxy.cfg", hap), ("keepalived.conf", keep)):
        shutil.copy2(path, backup + "/" + name)
    shutil.copy2(state_file, backup + "/lb-manager.json") if os.path.exists(state_file) else None
    try:
        with open(d + "/committed", "w") as marker:
            marker.flush()
            os.fsync(marker.fileno())
        sync_directory(d)
        atomic_copy(d + "/haproxy.new", hap)
        atomic_copy(d + "/keepalived.new", keep)
        atomic_copy(d + "/state.new", state_file)
        os.chmod(state_file, 0o600)
        run(["systemctl", "reload", "haproxy"])
        run(["systemctl", "reload", "keepalived"])
        run(["systemctl", "is-active", "haproxy"])
        run(["systemctl", "is-active", "keepalived"])
    except Exception:
        atomic_copy(backup + "/haproxy.cfg", hap)
        atomic_copy(backup + "/keepalived.conf", keep)
        if os.path.exists(backup + "/lb-manager.json"):
            atomic_copy(backup + "/lb-manager.json", state_file)
        else:
            try:
                os.unlink(state_file)
            except FileNotFoundError:
                pass
        run(["systemctl", "restart", "haproxy"], False)
        run(["systemctl", "reload", "keepalived"], False)
        try:
            os.unlink(d + "/committed")
        except FileNotFoundError:
            pass
        raise
    print(json.dumps({"committed": True, "backup": backup}))


def rollback(data):
    token = data["token"]
    d = transaction_path(token)
    if not os.path.isdir(d):
        raise RuntimeError("ops.change.incomplete")
    if os.path.isdir(d):
        if os.path.exists(d + "/committed"):
            if any(
                read_private(path, True)
                not in (
                    read_private(d + "/" + candidate),
                    read_private(d + "/" + original, True) if original else None,
                )
                for path, candidate, original in (
                    (hap, "haproxy.new", "haproxy.old"),
                    (keep, "keepalived.new", "keepalived.old"),
                    (
                        state_file,
                        "state.new",
                        "state.old" if os.path.exists(d + "/state.existed") else None,
                    ),
                )
            ):
                raise RuntimeError("ops.change.stale")
            atomic_copy(d + "/haproxy.old", hap)
            atomic_copy(d + "/keepalived.old", keep)
            if os.path.exists(d + "/state.existed"):
                atomic_copy(d + "/state.old", state_file)
            else:
                try:
                    os.unlink(state_file)
                except FileNotFoundError:
                    pass
            for service in ("haproxy", "keepalived"):
                _, code = run(["systemctl", "reload", service], False)
                if code:
                    run(["systemctl", "restart", service])
                run(["systemctl", "is-active", service])
    print(json.dumps({"rolledBack": True}))


def finish(data):
    d = transaction_path(data["token"])
    if not os.path.isfile(d + "/committed") or any(
        read_private(path) != read_private(d + "/" + candidate)
        for path, candidate in (
            (hap, "haproxy.new"),
            (keep, "keepalived.new"),
            (state_file, "state.new"),
        )
    ):
        raise RuntimeError("ops.change.stale")
    for service in ("haproxy", "keepalived"):
        run(["systemctl", "is-active", service])
    print(json.dumps({"finished": True}))


def complete(data, operation):
    """Keep recovery evidence until the durable reservation is released."""
    operation(data)
    release_operation(data["token"])
    path = transaction_path(data["token"])
    shutil.rmtree(path)
    sync_directory(str(Path(path).parent))


def main():
    try:
        raw = sys.stdin.readline(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("ops.error.limit")
        p = json.loads(raw)
        if not isinstance(p, dict):
            raise RuntimeError("ops.change.invalid_parameters")
        action = p.get("action")
        if action == "status":
            status()
            return 0
        owner = p.get("token")
        with host_operation(owner):
            if action == "prepare":
                if reservation() is not None:
                    raise RuntimeError("ops.change.incomplete")
                create_candidates(p, owner)
                reserve_operation(owner)
            elif action == "commit":
                if reservation() != owner:
                    raise RuntimeError("ops.host.reserved")
                commit(p)
            elif action == "rollback":
                if reservation() != owner:
                    raise RuntimeError("ops.host.reserved")
                complete(p, rollback)
            elif action == "finish":
                if reservation() != owner:
                    raise RuntimeError("ops.host.reserved")
                complete(p, finish)
            else:
                raise RuntimeError("ops.change.invalid_parameters")
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
