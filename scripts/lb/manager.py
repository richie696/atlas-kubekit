#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactive, bilingual manager for an existing Atlas KubeKit LB pair."""
from __future__ import annotations
import argparse
import base64
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.adapters.ssh import arguments as ssh_arguments

spec = importlib.util.spec_from_file_location("atlas_i18n", ROOT / "scripts/lib/i18n.py")
i18n = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = i18n
assert spec.loader
spec.loader.exec_module(i18n)

REMOTE_PATH = Path(__file__).with_name("remote.py")


class App:
    def __init__(self, lang: str, refresh: int, identity=None, ssh_config=None):
        self.lang = lang
        self.refresh = refresh
        self.identity = identity
        self.ssh_config = ssh_config
        self.path = Path.home() / ".config/atlas-kubekit/lb-manager.json"
        self.config = self.load()

    def t(self, key: str, *args) -> str:
        return i18n.translate(self.lang, key, *args)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {"sshUser": "ubuntu", "managed": [], "external": []}
        data.setdefault("sshUser", "ubuntu")
        data.setdefault("managed", [])
        data.setdefault("external", [])
        return data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def ask(self, key, default=""):
        suffix = f" [{default}]" if default else ""
        value = input(self.t(key) + suffix + ": ").strip()
        return value or default

    def ssh(self, node, payload):
        encoded = base64.b64encode(REMOTE_PATH.read_bytes()).decode()
        locking = base64.b64encode((ROOT / "scripts/lib/host_lock.py").read_bytes()).decode()
        files = base64.b64encode((ROOT / "scripts/lb/files.py").read_bytes()).decode()
        remote_code = (
            "import base64,sys,types; m=types.ModuleType('atlas_host_lock');"
            "sys.modules['atlas_host_lock']=m;"
            f"exec(base64.b64decode('{locking}'),m.__dict__);"
            "f=types.ModuleType('atlas_lb_files');sys.modules['atlas_lb_files']=f;"
            f"exec(base64.b64decode('{files}'),f.__dict__);"
            f"exec(base64.b64decode('{encoded}'))"
        )
        remote_command = "sudo -k -n python3 -c " + shlex.quote(remote_code)
        target = f"{self.config['sshUser']}@{node['ip']}"
        command = [*ssh_arguments(target, self.identity, self.ssh_config), target, remote_command]
        result = subprocess.run(
            command,
            input=json.dumps(payload) + "\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=150,
        )
        if result.returncode:
            raise RuntimeError(
                f"{node['name']} ({node['ip']}): {result.stdout.strip() or 'SSH/sudo failed'}"
            )
        try:
            return json.loads(result.stdout.splitlines()[-1])
        except Exception:
            raise RuntimeError(result.stdout.strip() or "Invalid response from remote node")

    def nodes(self):
        return self.config["managed"]

    def ensure_nodes(self):
        if len(self.nodes()) < 2:
            raise RuntimeError(self.t("lb.error.need_pair"))
        for node in self.nodes():
            status = self.ssh(node, {"action": "status"})
            if status.get("haproxy") != "active" or status.get("keepalived") != "active":
                raise RuntimeError(
                    self.t(
                        "lb.error.services",
                        node["name"],
                        status.get("haproxy"),
                        status.get("keepalived"),
                    )
                )
            if not status.get("haproxy_config") or not status.get("keepalived_config"):
                raise RuntimeError(self.t("lb.error.config", node["name"]))

    def get_states(self):
        states = []
        for node in self.nodes():
            states.append((node, self.ssh(node, {"action": "status"})))
        return states

    def dashboard(self):
        if not self.nodes():
            print(self.t("lb.nodes.empty"))
            input(self.t("lb.press_enter"))
            return
        try:
            while True:
                states = self.get_states()
                os.system("clear")
                print(self.t("lb.dashboard.title", time.strftime("%Y-%m-%d %H:%M:%S")))
                print("-" * 78)
                for node, s in states:
                    vip_owned = any(
                        ipaddress.ip_address(vip)
                        in [
                            ipaddress.ip_interface(x.split()[-1]).ip
                            for x in s.get("addresses", "").splitlines()
                            if "/" in x.split()[-1]
                        ]
                        for vip in s.get("state", {}).get("vips", [])
                    )
                    print(self.t("lb.dashboard.node", node["name"], node["ip"]))
                    print(self.t("lb.dashboard.addresses", s.get("addresses", "")))
                    print(
                        self.t(
                            "lb.dashboard.services",
                            s.get("haproxy"),
                            s.get("keepalived"),
                            self.t("lb.dashboard.vip_owner") if vip_owned else "",
                        )
                    )
                    print(
                        self.t(
                            "lb.dashboard.validation",
                            (
                                self.t("common.status.ok")
                                if s.get("haproxy_config")
                                else self.t("common.status.fail")
                            ),
                            (
                                self.t("common.status.ok")
                                if s.get("keepalived_config")
                                else self.t("common.status.fail")
                            ),
                        )
                    )
                    stats = s.get("stats", [])
                    if stats:
                        frontends = [r for r in stats if r.get("svname") == "FRONTEND"]
                        backends = [r for r in stats if r.get("svname") == "BACKEND"]
                        current = sum(int(r.get("scur") or 0) for r in frontends)
                        total = sum(int(r.get("stot") or 0) for r in frontends)
                        up = sum(1 for r in backends if r.get("status", "").startswith("UP"))
                        print(
                            self.t(
                                "lb.dashboard.stats",
                                len(frontends),
                                up,
                                len(backends),
                                current,
                                total,
                            )
                        )
                    else:
                        print(self.t("lb.dashboard.stats_unavailable"))
                print("-" * 78)
                print(
                    self.t(
                        "lb.dashboard.routes",
                        len(states[0][1].get("state", {}).get("routes", [])),
                        len(states[0][1].get("state", {}).get("vips", [])),
                    )
                )
                print(self.t("lb.dashboard.quit", self.refresh))
                time.sleep(self.refresh)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(self.t("lb.error.operation", str(e)))
            input(self.t("lb.press_enter"))

    def change_state(self, mutate):
        self.ensure_nodes()
        snapshots = self.get_states()
        states = [s.get("state", {"vips": [], "routes": []}) for _, s in snapshots]
        canonical = json.dumps(states[0], sort_keys=True)
        if any(json.dumps(s, sort_keys=True) != canonical for s in states[1:]):
            raise RuntimeError(self.t("lb.error.state_mismatch"))
        desired = json.loads(canonical)
        mutate(desired)
        print(self.t("lb.warning.test"))
        print(self.t("lb.warning.route_scope"))
        if input(self.t("lb.change.confirm") + " [no]: ").strip().lower() not in ("yes", "y", "是"):
            print(self.t("lb.change.cancelled"))
            return
        token = uuid.uuid4().hex
        prepared = []
        committed = []
        try:
            for node, _ in snapshots:
                self.ssh(
                    node,
                    {
                        "action": "prepare",
                        "state": desired,
                        "token": token,
                        "expectedRevision": next(
                            s["configurationRevision"] for n, s in snapshots if n == node
                        ),
                    },
                )
                prepared.append(node)
            for node in prepared:
                self.ssh(node, {"action": "commit", "token": token})
                committed.append(node)
        except BaseException:
            for node in reversed(prepared):
                try:
                    self.ssh(node, {"action": "rollback", "token": token})
                except Exception:
                    pass
            raise
        for node in prepared:
            self.ssh(node, {"action": "finish", "token": token})
        print(self.t("lb.change.success", len(committed)))

    def recover_transactions(self):
        states = self.get_states()
        pending = [
            (node, state["transactionOwner"])
            for node, state in states
            if state.get("transactionOwner")
        ]
        if not pending:
            print(self.t("lb.transaction.none"))
            return
        for node, owner in pending:
            print(node["name"] + ": " + owner)
        print(self.t("lb.transaction.guide"))
        action = self.ask("lb.transaction.action").strip().lower()
        if action not in ("rollback", "finish"):
            raise ValueError(self.t("ops.error.arguments"))
        if self.ask("lb.transaction.confirm") != "STOPPED LB COORDINATOR AND JOBS":
            raise ValueError(self.t("ops.error.confirm"))
        for node, owner in pending:
            self.ssh(node, {"action": action, "token": owner})
        print(self.t("lb.transaction.recovered"))

    def valid_name(self, value):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", value):
            raise ValueError(self.t("lb.error.name"))
        return value

    def vips_menu(self):
        while True:
            print("\n" + self.t("lb.vip.title"))
            print("1) " + self.t("lb.vip.add"))
            print("2) " + self.t("lb.vip.remove"))
            print("3) " + self.t("lb.vip.list"))
            print("0) " + self.t("common.menu.back"))
            choice = input(self.t("common.menu.choice") + ": ").strip()
            try:
                if choice == "1":
                    vip = self.ipv4(self.ask("lb.vip.prompt"))
                    if vip == "10.20.1.9":
                        raise ValueError(self.t("lb.error.protected_vip"))

                    def add(s):
                        if vip in s["vips"]:
                            raise ValueError(self.t("lb.error.exists"))
                        s["vips"].append(vip)
                        s["vips"].sort(key=lambda x: int(ipaddress.IPv4Address(x)))

                    self.change_state(add)
                elif choice == "2":
                    state = self.get_states()[0][1].get("state", {})
                    print(", ".join(state.get("vips", [])) or self.t("lb.list.empty"))
                    vip = self.ask("lb.vip.remove_prompt")

                    def rem(s):
                        if vip not in s["vips"]:
                            raise ValueError(self.t("lb.error.not_found"))
                        s["vips"].remove(vip)

                    self.change_state(rem)
                elif choice == "3":
                    state = self.get_states()[0][1].get("state", {})
                    print("\n".join(state.get("vips", [])) or self.t("lb.list.empty"))
                    input(self.t("lb.press_enter"))
                elif choice == "0":
                    return
            except (KeyboardInterrupt, EOFError):
                return
            except Exception as e:
                print(self.t("lb.error.operation", str(e)))

    def route_menu(self):
        while True:
            print("\n" + self.t("lb.route.title"))
            print("1) " + self.t("lb.route.add"))
            print("2) " + self.t("lb.route.remove"))
            print("3) " + self.t("lb.route.list"))
            print("0) " + self.t("common.menu.back"))
            choice = input(self.t("common.menu.choice") + ": ").strip()
            try:
                if choice == "1":
                    name = self.valid_name(self.ask("lb.route.name"))
                    mode = self.ask("lb.route.mode", "http").lower()
                    if mode not in ("http", "tcp"):
                        raise ValueError(self.t("lb.error.mode"))
                    raw_port = self.ask("lb.route.port")
                    try:
                        port = int(raw_port)
                    except ValueError:
                        raise ValueError(self.t("lb.error.port")) from None
                    if not 1 <= port <= 65535:
                        raise ValueError(self.t("lb.error.port"))
                    # Bind on all local addresses so the standby LB can accept a VIP after failover.
                    vip = "*"
                    raw = self.ask("lb.route.backends")
                    backends = []
                    for item in raw.split(","):
                        addr, sep, p = item.strip().rpartition(":")
                        if not sep:
                            raise ValueError(self.t("lb.error.backend_format"))
                        addr = self.ipv4(addr)
                        try:
                            p = int(p)
                        except ValueError:
                            raise ValueError(self.t("lb.error.port")) from None
                        if not 1 <= p <= 65535:
                            raise ValueError(self.t("lb.error.port"))
                        backends.append(f"{addr}:{p}")
                    if not backends:
                        raise ValueError(self.t("lb.error.backends"))
                    ws = mode == "http" and self.ask("lb.route.websocket", "no").lower() in (
                        "yes",
                        "y",
                        "是",
                    )

                    def add(s):
                        if any(r["name"] == name for r in s["routes"]):
                            raise ValueError(self.t("lb.error.exists"))
                        if any(r["listenPort"] == port and r["bind"] == vip for r in s["routes"]):
                            raise ValueError(self.t("lb.error.port_conflict"))
                        s["routes"].append(
                            {
                                "name": name,
                                "mode": mode,
                                "listenPort": port,
                                "bind": vip,
                                "backends": backends,
                                "websocket": ws,
                            }
                        )

                    self.change_state(add)
                elif choice == "2":
                    state = self.get_states()[0][1].get("state", {})
                    print(
                        "\n".join(r["name"] for r in state.get("routes", []))
                        or self.t("lb.list.empty")
                    )
                    name = self.ask("lb.route.remove_prompt")

                    def rem(s):
                        rows = [r for r in s["routes"] if r["name"] == name]
                        if not rows:
                            raise ValueError(self.t("lb.error.not_found"))
                        s["routes"] = [r for r in s["routes"] if r["name"] != name]

                    self.change_state(rem)
                elif choice == "3":
                    state = self.get_states()[0][1].get("state", {})
                    print(
                        json.dumps(state.get("routes", []), ensure_ascii=False, indent=2)
                        or self.t("lb.list.empty")
                    )
                    input(self.t("lb.press_enter"))
                elif choice == "0":
                    return
            except (KeyboardInterrupt, EOFError):
                return
            except Exception as e:
                print(self.t("lb.error.operation", str(e)))

    def nodes_menu(self):
        while True:
            print("\n" + self.t("lb.nodes.title"))
            print("1) " + self.t("lb.nodes.replace"))
            print("2) " + self.t("lb.nodes.add"))
            print("3) " + self.t("lb.nodes.remove"))
            print("4) " + self.t("lb.nodes.ssh_user"))
            print("0) " + self.t("common.menu.back"))
            choice = input(self.t("common.menu.choice") + ": ").strip()
            try:
                if choice == "1":
                    raw = self.ask("lb.nodes.batch")
                    parsed = self.parse_nodes(raw)
                    if len(parsed) < 2:
                        raise ValueError(self.t("lb.error.need_pair"))
                    self.config["managed"] = parsed
                    self.save()
                    print(self.t("lb.nodes.saved"))
                elif choice == "2":
                    parsed = self.parse_nodes(self.ask("lb.nodes.batch"))
                    have = {n["name"] for n in self.nodes()}
                    for n in parsed:
                        if n["name"] not in have:
                            self.nodes().append(n)
                    self.save()
                    print(self.t("lb.nodes.saved"))
                elif choice == "3":
                    print(", ".join(n["name"] + "=" + n["ip"] for n in self.nodes()))
                    name = self.ask("lb.nodes.remove_prompt")
                    proposed = [n for n in self.nodes() if n["name"] != name]
                    if len(proposed) < 2:
                        raise ValueError(self.t("lb.error.need_pair"))
                    self.config["managed"] = proposed
                    self.save()
                    print(self.t("lb.nodes.saved"))
                elif choice == "4":
                    user = self.ask(
                        "lb.nodes.ssh_user_prompt", self.config.get("sshUser", "ubuntu")
                    )
                    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
                        raise ValueError(self.t("lb.error.ssh_user"))
                    self.config["sshUser"] = user
                    self.save()
                    print(self.t("lb.nodes.saved"))
                elif choice == "0":
                    return
            except (KeyboardInterrupt, EOFError):
                return
            except Exception as e:
                print(self.t("lb.error.operation", str(e)))

    def ipv4(self, value):
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            raise ValueError(self.t("lb.error.ipv4")) from None

    def parse_nodes(self, raw):
        result = []
        for entry in re.split(r"[\s,;]+", raw.strip()):
            if not entry:
                continue
            name, sep, ip = entry.partition("=")
            if not sep or not re.fullmatch(r"lb[a-z0-9-]*", name):
                raise ValueError(self.t("lb.error.node_format"))
            result.append({"name": name, "ip": self.ipv4(ip)})
        if len({x["name"] for x in result}) != len(result) or len({x["ip"] for x in result}) != len(
            result
        ):
            raise ValueError(self.t("lb.error.node_duplicate"))
        return result

    def external_menu(self):
        while True:
            print("\n" + self.t("lb.external.title"))
            print("1) " + self.t("lb.external.add"))
            print("2) " + self.t("lb.external.remove"))
            print("3) " + self.t("lb.external.list"))
            print("0) " + self.t("common.menu.back"))
            choice = input(self.t("common.menu.choice") + ": ").strip()
            try:
                if choice == "1":
                    name = self.valid_name(self.ask("lb.external.name"))
                    endpoint = self.ask("lb.external.endpoint")
                    if not re.match(r"^https?://", endpoint) and not re.match(
                        r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$", endpoint
                    ):
                        raise ValueError(self.t("lb.error.endpoint"))
                    self.config["external"] = [
                        x for x in self.config["external"] if x["name"] != name
                    ] + [{"name": name, "endpoint": endpoint}]
                    self.save()
                    print(self.t("lb.external.saved"))
                elif choice == "2":
                    print(
                        "\n".join(x["name"] + " " + x["endpoint"] for x in self.config["external"])
                        or self.t("lb.list.empty")
                    )
                    name = self.ask("lb.external.remove_prompt")
                    self.config["external"] = [
                        x for x in self.config["external"] if x["name"] != name
                    ]
                    self.save()
                    print(self.t("lb.external.saved"))
                elif choice == "3":
                    print(
                        "\n".join(x["name"] + " " + x["endpoint"] for x in self.config["external"])
                        or self.t("lb.list.empty")
                    )
                    input(self.t("lb.press_enter"))
                elif choice == "0":
                    return
            except (KeyboardInterrupt, EOFError):
                return
            except Exception as e:
                print(self.t("lb.error.operation", str(e)))

    def language_menu(self):
        locales = i18n.available_locales()
        print(self.t("common.language.prompt"))
        for idx, (code, name) in enumerate(locales.items(), 1):
            print(f"{idx}) {i18n.translate(self.lang,'common.language.'+code.lower())} ({code})")
        choice = input(self.t("common.menu.choice") + ": ").strip()
        codes = list(locales)
        if choice.isdigit() and 1 <= int(choice) <= len(codes):
            self.lang = codes[int(choice) - 1]
        elif choice in locales:
            self.lang = choice
        print(i18n.translate(self.lang, "common.language.selected", self.lang))

    def run(self):
        print(self.t("lb.warning.test"))
        print(self.t("lb.warning.route_scope"))
        while True:
            print("\n" + self.t("lb.menu.title"))
            print("1) " + self.t("lb.menu.status"))
            print("2) " + self.t("lb.menu.vips"))
            print("3) " + self.t("lb.menu.routes"))
            print("4) " + self.t("lb.menu.nodes"))
            print("5) " + self.t("lb.menu.external"))
            print("6) " + self.t("lb.transaction.menu"))
            print("L) " + self.t("common.language.prompt"))
            print("0) " + self.t("common.menu.exit"))
            choice = input(self.t("common.menu.choice") + ": ").strip().lower()
            try:
                if choice == "1":
                    self.dashboard()
                elif choice == "2":
                    self.vips_menu()
                elif choice == "3":
                    self.route_menu()
                elif choice == "4":
                    self.nodes_menu()
                elif choice == "5":
                    self.external_menu()
                elif choice == "6":
                    self.recover_transactions()
                elif choice == "l":
                    self.language_menu()
                elif choice == "0":
                    return
            except Exception as e:
                print(self.t("lb.error.operation", str(e)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--refresh", type=int, default=3)
    parser.add_argument("--ssh-identity")
    parser.add_argument("--ssh-config")
    args = parser.parse_args()
    try:
        if args.refresh < 1 or args.refresh > 60:
            raise ValueError("refresh must be 1..60")
        App(args.lang, args.refresh, args.ssh_identity, args.ssh_config).run()
    except (i18n.LocalizationError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
