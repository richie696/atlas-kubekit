#!/usr/bin/env python3
"""Check the node collector lifecycle in a disposable Linux systemd container."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations.adapters.host import BOOTSTRAP, bundle


PREFIX = "/etc/systemd/system/atlas-node-systemd-metrics"
TEXTFILES = "/var/lib/atlas-kubekit/node-exporter-textfile"


def command(*arguments, data=None, check=True):
    return subprocess.run(["docker", *arguments], input=data, capture_output=True, text=True,
                          check=check, timeout=90)


def verify(container):
    def run(*arguments):
        return command("exec", container, *arguments).stdout

    code = bundle()

    def host(action, parameters=None, error=None):
        request = dict(schemaVersion=1, action="systemd-metrics-" + action,
                       parameters=parameters or {}, runId="a" * 64)
        result = command("exec", "-i", container, "python3", "-c", BOOTSTRAP,
                         data=json.dumps({"bundle": code, "request": request}) + "\n", check=False)
        value = json.loads(result.stdout)
        if error:
            assert result.returncode == 1 and value == {"ok": False, "error": error, "values": []}, value
            return
        assert result.returncode == 0 and value["ok"] is True, value
        return value["result"]

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if command("exec", container, "systemctl", "show", "--property=Version", check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("container systemd did not start")

    for unit in ("kubelet", "containerd"):
        run("python3", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
            "/etc/systemd/system/" + unit + ".service",
            "[Service]\nType=simple\nExecStart=/bin/sleep infinity\n")
    run("systemctl", "daemon-reload")
    run("systemctl", "start", "kubelet", "containerd")
    service_state = run("systemctl", "show", "--property=ActiveState,MainPID", "kubelet", "containerd")
    assert service_state.count("ActiveState=active") == 2

    host("apply")
    previous = host("observe-removal")
    identity = {key: previous["identity"][key] for key in ("hostname", "identityFingerprint")}
    params = {"identity": identity, "expected": previous["files"]}
    assert all(params["expected"].values())

    run("touch", TEXTFILES + "/other.prom")
    run("python3", "-c", "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "p.write_text(p.read_text() + '\\n# changed since preview\\n')", PREFIX + ".timer")
    host("remove", params, error="ops.host.failed")
    assert run("systemctl", "is-active", PREFIX.rsplit("/", 1)[1] + ".timer").strip() == "active"
    run("mv", PREFIX + ".timer", PREFIX + ".timer.saved")
    run("ln", "-s", PREFIX + ".timer.saved", PREFIX + ".timer")
    host("observe-removal", error="ops.host.failed")
    run("test", "-L", PREFIX + ".timer")
    run("mv", "-T", PREFIX + ".timer.saved", PREFIX + ".timer")
    host("observe-removal")

    def remove(current):
        result = host("remove", {"identity": identity, "expected": current["files"]})
        assert result["files"] == dict.fromkeys(params["expected"])

    remove(host("observe-removal"))
    run("test", "!", "-e", TEXTFILES + "/atlas_systemd_units.prom")
    run("test", "-f", TEXTFILES + "/other.prom")
    assert run("systemctl", "show", "--property=ActiveState,MainPID", "kubelet", "containerd") == service_state
    remove(host("observe-removal"))
    host("apply")
    assert "atlas_systemd_unit_active" in run("cat", TEXTFILES + "/atlas_systemd_units.prom")
    remove(host("observe-removal"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local Linux image with systemd and Python installed")
    parser.add_argument("--confirm", required=True, choices=["atlas-monitor-e2e"])
    args = parser.parse_args()
    image_id = command("image", "inspect", "--format", "{{.Id}}", "--", args.image).stdout.strip()
    suffix = uuid.uuid4().hex[:10]
    container = "atlas-systemd-remove-" + suffix
    command("run", "--detach", "--pull=never", "--network=none", "--name", container,
            "--hostname", "cp1", "--privileged", "--tmpfs", "/run", "--tmpfs", "/run/lock",
            image_id, "/sbin/init")
    try:
        verify(container)
        print("Systemd collector install, drift and symlink rejection, uninstall, replay and reinstall passed.")
    finally:
        command("rm", "-f", container)
        if command("container", "inspect", container, check=False).returncode == 0:
            raise RuntimeError("systemd test container still exists")
        print("Temporary systemd container cleanup verified.")


if __name__ == "__main__":
    main()
