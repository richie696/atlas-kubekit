#!/usr/bin/env python3
"""Exercise the independent observer in a disposable Ubuntu systemd container."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
CONFIRMATION = "atlas-monitor-e2e"
HEALTH_PORT = 18081
STATE_DIRECTORY = "/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http"
UNIT_NAME = "atlas-kubekit-observer-e2e-http.service"
FAILURE_UNIT_NAME = "atlas-kubekit-observer-failure-e2e-http.service"
WEBHOOK_LOG = "/tmp/atlas-observer-webhook.jsonl"

INSPECT_HELPER = r'''import json, sys
sys.path.insert(0, "/opt/atlas-kubekit")
from scripts.operations.monitoring.observer.status import snapshot
print(json.dumps(snapshot("/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http")))
'''

WEBHOOK_HELPER = r'''import json, ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with open("/tmp/atlas-observer-webhook.jsonl", "a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        body = b'{"errcode":0,"errmsg":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/tmp/qyapi.crt", "/tmp/qyapi.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''

def run(arguments, *, env=None, timeout=120, check=True, input_text=None):
    result = subprocess.run(arguments, cwd=ROOT, env=env, text=True,
                            input=input_text, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(arguments) + "\n" +
                           result.stdout[-4000:] + result.stderr[-4000:])
    return result


def container_exec(name, *arguments, user=None, user_bus=False, check=True, timeout=30):
    command = ["docker", "exec"]
    if user:
        command.extend(("--user", user))
    if user_bus:
        command.extend(("--env", "HOME=/home/ubuntu", "--env", "XDG_RUNTIME_DIR=/run/user/1000",
                        "--env", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"))
    return run(command + [name, *arguments], timeout=timeout, check=check)


def write_helper(directory, name, content):
    path = directory / name
    path.write_text(content)
    return path


def assert_private_paths(name):
    script = r'''import os, stat
paths = {
 "/home/ubuntu/.config/systemd/user/atlas-kubekit-observer-e2e-http.service": (stat.S_IMODE(os.stat("/home/ubuntu/.config/systemd/user/atlas-kubekit-observer-e2e-http.service").st_mode), 0o600),
 "/home/ubuntu/.config/systemd/user/atlas-kubekit-observer-failure-e2e-http.service": (stat.S_IMODE(os.stat("/home/ubuntu/.config/systemd/user/atlas-kubekit-observer-failure-e2e-http.service").st_mode), 0o600),
 "/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http": (stat.S_IMODE(os.stat("/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http").st_mode), 0o700),
 "/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http/observer.sqlite": (stat.S_IMODE(os.stat("/home/ubuntu/.local/state/atlas-kubekit/observer/e2e-http/observer.sqlite").st_mode), 0o600),
}
assert all(actual == expected for actual, expected in paths.values()), paths
'''
    container_exec(name, "python3", "-c", script, user="ubuntu")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        parser.error("Explicit confirmation " + CONFIRMATION + " is required")

    suffix = uuid.uuid4().hex[:10]
    container = "atlas-observer-e2e-" + suffix
    image = "atlas-kubekit-observer-e2e:" + suffix
    label = "atlas-kubekit.e2e=" + suffix
    context = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-observer-e2e-", dir=context) as temp:
        directory = Path(temp)
        docker_config = directory / "docker-config"
        docker_config.mkdir(mode=0o700)
        environment = dict(os.environ)
        environment["DOCKER_CONFIG"] = str(docker_config)
        dockerfile = directory / "Dockerfile"
        dockerfile.write_text(
            "FROM ubuntu:24.04\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "systemd systemd-sysv dbus libpam-systemd python3 ca-certificates sudo openssl "
            "&& usermod -aG sudo ubuntu "
            "&& printf 'ubuntu ALL=(root) NOPASSWD: ALL\\n' > /etc/sudoers.d/atlas-e2e "
            "&& chmod 0440 /etc/sudoers.d/atlas-e2e "
            "&& rm -rf /var/lib/apt/lists/*\n"
            "STOPSIGNAL SIGRTMIN+3\n"
            "CMD [\"/sbin/init\"]\n"
        )
        helpers = {
            "inspect_observer.py": write_helper(directory, "inspect_observer.py", INSPECT_HELPER),
            "fake_wecom.py": write_helper(directory, "fake_wecom.py", WEBHOOK_HELPER),
        }
        started = False
        try:
            run(["docker", "build", "--pull=false", "-t", image, "-f", str(dockerfile), str(directory)],
                env=environment, timeout=900)
            run(["docker", "run", "-d", "--name", container, "--label", label, "--privileged",
                 "--tmpfs", "/run", "--tmpfs", "/run/lock", "-v", str(ROOT) + ":/opt/atlas-kubekit:ro",
                 image], env=environment)
            started = True

            deadline = time.monotonic() + 60
            while True:
                status = container_exec(container, "systemctl", "is-system-running", "--wait", check=False)
                if status.returncode == 0 and status.stdout.strip() in ("running", "degraded"):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("systemd did not become ready: " + status.stdout + status.stderr)
                time.sleep(1)
            # A user manager must exist before the installer can configure it.
            # On a real host this is supplied by the active login; this disposable
            # container has no login session, so bootstrap the manager explicitly.
            container_exec(container, "loginctl", "enable-linger", "ubuntu")
            deadline = time.monotonic() + 30
            while True:
                user_status = container_exec(container, "loginctl", "user-status", "ubuntu", "--no-pager",
                                             check=False)
                if user_status.returncode == 0 and "user@1000.service" in user_status.stdout:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("systemd user manager did not start: " + user_status.stdout + user_status.stderr)
                time.sleep(1)

            # Start the local fixture detached; no external webhook message can fire
            # because the configured failure threshold is deliberately very high.
            run(["docker", "exec", "--detach", container, "python3", "-m", "http.server",
                 str(HEALTH_PORT), "--bind", "127.0.0.1"], env=environment)
            endpoint_writer = (
                "from pathlib import Path; import os; "
                "p=Path('/home/ubuntu/.config/atlas-kubekit'); p.mkdir(parents=True, exist_ok=True); "
                "f=p/'e2e-webhook'; f.write_text('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-only'); "
                "f.chmod(0o600)"
            )
            container_exec(container, "python3", "-c", endpoint_writer, user="ubuntu")
            for path, helper in helpers.items():
                run(["docker", "cp", str(helper), container + ":/tmp/" + path], env=environment)
            container_exec(container, "bash", "-lc",
                           "openssl req -x509 -newkey rsa:2048 -nodes -days 1 "
                           "-keyout /tmp/qyapi.key -out /tmp/qyapi.crt "
                           "-subj /CN=qyapi.weixin.qq.com "
                           "-addext subjectAltName=DNS:qyapi.weixin.qq.com && "
                           "printf '127.0.0.1 qyapi.weixin.qq.com\\n' >> /etc/hosts && "
                           "chmod 600 /tmp/qyapi.key && "
                           "touch /tmp/atlas-observer-webhook.jsonl && chmod 666 /tmp/atlas-observer-webhook.jsonl")
            run(["docker", "exec", "-d", container, "python3", "/tmp/fake_wecom.py"], env=environment)
            ca_writer = (
                "from pathlib import Path; import os; "
                "p=Path('/home/ubuntu/.config/atlas-kubekit'); p.mkdir(parents=True, exist_ok=True); "
                "f=p/'ca.pem'; f.write_bytes(Path('/tmp/qyapi.crt').read_bytes()); f.chmod(0o600)"
            )
            container_exec(container, "python3", "-c", ca_writer, user="ubuntu")
            container_exec(container, "systemctl", "--user", "set-environment",
                           "SSL_CERT_FILE=/home/ubuntu/.config/atlas-kubekit/ca.pem",
                           user="ubuntu", user_bus=True)
            container_exec(container, "python3", "-c", "import time; time.sleep(1)")
            install = container_exec(
                container, "bash", "/opt/atlas-kubekit/06-maintain-cluster.sh",
                "--lang", "zh_CN", "--monitoring-observer-install",
                "--observer-name", "e2e-http",
                "--observer-url", "http://127.0.0.1:" + str(HEALTH_PORT) + "/",
                "--observer-state-directory", STATE_DIRECTORY,
                "--observer-wecom-endpoint-file", "/home/ubuntu/.config/atlas-kubekit/e2e-webhook",
                "--observer-interval", "2", "--observer-fail-after", "1000",
                "--observer-recover-after", "2", user="ubuntu", user_bus=True)
            if "已安装并启动" not in install.stdout:
                raise RuntimeError("06-maintain-cluster observer install did not report success: " + install.stdout)

            deadline = time.monotonic() + 30
            observation = None
            while time.monotonic() < deadline:
                active = container_exec(container, "systemctl", "--user", "is-active", UNIT_NAME,
                                        user="ubuntu", user_bus=True, check=False)
                inspect = container_exec(container, "python3", "/tmp/inspect_observer.py", user="ubuntu", check=False)
                if active.returncode == 0 and inspect.returncode == 0:
                    try:
                        observation = json.loads(inspect.stdout)
                    except ValueError:
                        observation = None
                    if (observation and observation.get("observations") == [["e2e-http", "healthy", "healthy"]]
                            and observation.get("pending") == 0
                            and observation.get("probes")
                            and observation["probes"][0][1] == "expected-http-status"):
                        break
                time.sleep(1)
            else:
                raise RuntimeError("observer systemd service did not persist a healthy probe")
            assert_private_paths(container)

            status = container_exec(
                container, "python3", "/opt/atlas-kubekit/scripts/operations/cli.py",
                "--lang", "zh_CN", "--monitoring-observer-status",
                "--observer-state-directory", STATE_DIRECTORY,
                user="ubuntu", check=False)
            if status.returncode != 2 or "观察对象 e2e-http：事件状态 正常；最近观察 正常" not in status.stdout:
                raise RuntimeError("observer status command did not report the healthy probe: " + status.stdout + status.stderr)

            # Freeze the observer process. systemd must detect the missed watchdog
            # heartbeat, restart it, and invoke the separate failure notification.
            pid = container_exec(container, "systemctl", "--user", "show", "--property=MainPID", "--value",
                                 UNIT_NAME, user="ubuntu", user_bus=True).stdout.strip()
            if not pid.isdigit() or int(pid) <= 1:
                raise RuntimeError("could not identify observer main process for watchdog E2E")
            container_exec(container, "kill", "-STOP", pid)
            deadline = time.monotonic() + 75
            rows = []
            failure_payload = None
            while time.monotonic() < deadline:
                inspect_webhook = container_exec(
                    container, "python3", "-c",
                    "import json; from pathlib import Path; p=Path('" + WEBHOOK_LOG + "'); "
                    "rows=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]; "
                    "print(json.dumps(rows, ensure_ascii=False))",
                    check=False)
                try:
                    rows = json.loads(inspect_webhook.stdout)
                except (TypeError, ValueError):
                    rows = []
                messages = [item.get("text", {}).get("content", "") for item in rows]
                failure_payload = next((text for text in messages if "独立观察器故障" in text), None)
                service_state = container_exec(container, "systemctl", "--user", "is-active", UNIT_NAME,
                                               user="ubuntu", user_bus=True, check=False)
                if failure_payload and service_state.stdout.strip() == "active":
                    break
                time.sleep(1)
            else:
                journal = container_exec(container, "journalctl", "--user", "-u", UNIT_NAME,
                                         "-u", FAILURE_UNIT_NAME, "--no-pager", "-n", "80",
                                         user="ubuntu", user_bus=True, check=False)
                raise RuntimeError("systemd watchdog did not notify/restart observer: " +
                                   journal.stdout[-5000:] + journal.stderr[-1000:])
            if "e2e-http" not in failure_payload or "systemd 日志" not in failure_payload:
                raise RuntimeError("observer process failure notification lacks localized diagnostic content")
            restarts = container_exec(container, "systemctl", "--user", "show", "--property=NRestarts", "--value",
                                      UNIT_NAME, user="ubuntu", user_bus=True).stdout.strip()
            if not restarts.isdigit() or int(restarts) < 1:
                raise RuntimeError("systemd did not restart the failed observer process")
            before_remove = len(rows)

            remove = container_exec(
                container, "bash", "/opt/atlas-kubekit/06-maintain-cluster.sh",
                "--lang", "zh_CN", "--monitoring-observer-remove",
                "--observer-name", "e2e-http", "--confirm", "e2e-http",
                user="ubuntu", user_bus=True)
            if "已停止、禁用并移除" not in remove.stdout or "状态数据和 Webhook 文件已保留" not in remove.stdout:
                raise RuntimeError("06-maintain-cluster observer remove did not report safe cleanup: " + remove.stdout)
            inactive = container_exec(container, "systemctl", "--user", "is-active", UNIT_NAME,
                                      user="ubuntu", user_bus=True, check=False)
            if inactive.returncode == 0 or inactive.stdout.strip() != "inactive":
                raise RuntimeError("observer service remained active after removal")
            failure_inactive = container_exec(container, "systemctl", "--user", "is-active", FAILURE_UNIT_NAME,
                                              user="ubuntu", user_bus=True, check=False)
            if failure_inactive.returncode == 0 or failure_inactive.stdout.strip() != "inactive":
                raise RuntimeError("observer failure notification unit remained active after removal")
            if container_exec(container, "test", "-e", "/home/ubuntu/.config/systemd/user/" + FAILURE_UNIT_NAME,
                              user="ubuntu", check=False).returncode == 0:
                raise RuntimeError("observer failure notification unit file remained after removal")
            if container_exec(container, "test", "-f", STATE_DIRECTORY + "/observer.sqlite",
                              user="ubuntu", check=False).returncode != 0:
                raise RuntimeError("observer state was removed with the unit")
            time.sleep(2)
            after = container_exec(container, "python3", "-c",
                                   "from pathlib import Path; print(sum(bool(x.strip()) for x in Path('" +
                                   WEBHOOK_LOG + "').read_text().splitlines()))").stdout.strip()
            if not after.isdigit() or int(after) != before_remove:
                raise RuntimeError("clean observer removal unexpectedly sent a failure notification")
            print("Ubuntu 24.04 systemd observer watchdog, independent failure notification, restart, "
                  "healthy probe, private state, and safe removal passed.")
        finally:
            if started:
                run(["docker", "rm", "-f", container], env=environment, check=False)
            removed_image = run(["docker", "rmi", image], env=environment, check=False)
            if removed_image.returncode != 0:
                raise RuntimeError("could not remove temporary observer E2E image: " + removed_image.stderr[-1000:])
            owned = run(["docker", "ps", "-a", "--filter", "label=" + label, "--format", "{{.ID}}"],
                        env=environment)
            if owned.stdout.strip():
                raise RuntimeError("owned observer E2E container remains after cleanup")
            remaining_image = run(["docker", "image", "inspect", image], env=environment, check=False)
            if remaining_image.returncode == 0:
                raise RuntimeError("temporary observer E2E image remains after cleanup")


if __name__ == "__main__":
    main()
