# SPDX-License-Identifier: MIT
"""Install and remove a per-user systemd service for an independent observer."""
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from scripts.lib.i18n import available_locales
from ..channels import secret_file, wecom
from . import dns_probe, probe

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_MARKER = "# Atlas KubeKit managed independent observer"
_FAILURE_MARKER = "# Atlas KubeKit managed independent observer failure notifier"
_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-")


def _systemd_escape(value):
    """Escape one systemd unit-file value without shell interpretation."""
    encoded = str(value).encode("utf-8")
    parts = []
    for byte in encoded:
        if byte in _SAFE:
            parts.append(chr(byte))
        elif byte == 0x25:
            parts.append("%%")
        elif byte == 0x24:
            parts.append("$$")
        else:
            parts.append("\\x" + format(byte, "02x"))
    return "".join(parts)


def _systemd_quote(value):
    """Quote one ExecStart argument without invoking a shell."""
    return '"' + _systemd_escape(value) + '"'


def normalize(config, home=None, owner=None):
    """Validate observer settings before touching local files."""
    required = {
        "name", "state_directory", "url", "dns_server", "dns_port", "dns_name", "dns_expected",
        "wecom_endpoint_file", "lang", "interval", "fail_after", "recover_after",
    }
    if (not isinstance(config, dict) or not required.issubset(config) or
            set(config) - (required | {"ca_file"})):
        raise ValueError("invalid observer configuration")
    config = dict(config)
    config.setdefault("ca_file", None)
    name = config["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid observer name")
    if config["lang"] not in available_locales():
        raise ValueError("invalid observer language")
    for key, low, high in (("interval", 1, 3600), ("fail_after", 1, 1000), ("recover_after", 1, 1000)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("invalid observer interval policy")
    state = config["state_directory"]
    if state is not None and (not isinstance(state, str) or not os.path.isabs(state)):
        raise ValueError("invalid observer state path")
    endpoint_file = config["wecom_endpoint_file"]
    if not isinstance(endpoint_file, str) or not os.path.isabs(endpoint_file):
        raise ValueError("invalid WeCom endpoint file")
    wecom._endpoint(secret_file.read_private(endpoint_file))
    url = config["url"]
    dns_fields = (config["dns_server"], config["dns_name"], config["dns_expected"])
    if url is not None:
        if any(value is not None for value in dns_fields) or type(config["dns_port"]) is not int:
            raise ValueError("choose one observer target")
        probe.validate_url(url)
        result_ca_file = probe.validate_ca_file(config["ca_file"])
    else:
        if config["ca_file"] is not None:
            raise ValueError("CA bundle is only valid for HTTP probes")
        result_ca_file = None
        server, name_value, expected = dns_fields
        if not server or not name_value or not isinstance(expected, list):
            raise ValueError("incomplete DNS observer target")
        dns_probe.validate(server, name_value, expected)
        if type(config["dns_port"]) is not int or not 1 <= config["dns_port"] <= 65535:
            raise ValueError("invalid DNS port")
    result = dict(config)
    result["ca_file"] = result_ca_file
    result["state_directory"] = str(Path(state or (Path(home or Path.home()) / ".local/state/atlas-kubekit/observer" / name)).absolute())
    result["wecom_endpoint_file"] = str(Path(endpoint_file).absolute())
    return result


def render_unit(config, python=sys.executable, module_root=None):
    """Render a hardened user unit for an already validated configuration."""
    root = Path(module_root or Path(__file__).resolve().parents[4]).absolute()
    service = [python, "-m", "scripts.operations.monitoring.observer.service",
               "--state-directory", config["state_directory"], "--name", config["name"]]
    if config["url"] is not None:
        service.extend(("--url", config["url"]))
        if config["ca_file"] is not None:
            service.extend(("--ca-file", config["ca_file"]))
    else:
        service.extend(("--dns-server", config["dns_server"], "--dns-port", str(config["dns_port"]),
                        "--dns-name", config["dns_name"]))
        for address in config["dns_expected"]:
            service.extend(("--dns-expected", address))
    service.extend(("--wecom-endpoint-file", config["wecom_endpoint_file"], "--lang", config["lang"],
                    "--interval", str(config["interval"]), "--fail-after", str(config["fail_after"]),
                    "--recover-after", str(config["recover_after"])))
    unit = [
        _MARKER,
        "[Unit]",
        "Description=Atlas KubeKit independent observer " + config["name"],
        "After=network.target",
        "OnFailure=atlas-kubekit-observer-failure-" + config["name"] + ".service",
        "",
        "[Service]",
        "Type=notify",
        "NotifyAccess=main",
        "WatchdogSec=" + str(config["interval"] + 20) + "s",
        "TimeoutStartSec=45s",
        # WorkingDirectory= expects a path value, not an ExecStart-style
        # quoted argument; quoting makes systemd treat the quote as path data.
        "WorkingDirectory=" + _systemd_escape(root),
        "ExecStart=" + " ".join(_systemd_quote(arg) for arg in service),
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        "Restart=on-failure",
        "RestartSec=10s",
        "TimeoutStopSec=15s",
        "UMask=0077",
        "NoNewPrivileges=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(unit)


def render_failure_unit(config, python=sys.executable, module_root=None):
    """Render an independent oneshot that reports process/watchdog failures."""
    root = Path(module_root or Path(__file__).resolve().parents[4]).absolute()
    service = [python, "-m", "scripts.operations.monitoring.observer.failure_notify",
               "--name", config["name"], "--endpoint-file", config["wecom_endpoint_file"],
               "--lang", config["lang"]]
    unit = [
        _FAILURE_MARKER,
        "[Unit]",
        "Description=Atlas KubeKit independent observer failure notification " + config["name"],
        "After=network.target",
        "",
        "[Service]",
        "Type=oneshot",
        "WorkingDirectory=" + _systemd_escape(root),
        "ExecStart=" + " ".join(_systemd_quote(arg) for arg in service),
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        "TimeoutStartSec=45s",
        "UMask=0077",
        "NoNewPrivileges=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "",
    ]
    return "\n".join(unit)


def _private_directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("observer directory must be private and user-owned")
    return path


def _write_owned(path, text, marker=_MARKER):
    existing = None
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("observer unit is not an owned private file")
        existing = path.read_text()
        if marker not in existing:
            raise ValueError("refusing to replace a foreign systemd unit")
    except FileNotFoundError:
        pass
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return existing


def _systemctl(runner, *args):
    if not shutil.which("systemctl"):
        raise ValueError("systemd user manager is unavailable")
    result = runner(["systemctl", "--user", *args], check=False, capture_output=True, text=True)
    return result.returncode == 0


def _restore_unit(path, previous, marker):
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        _write_owned(path, previous, marker)


def _ensure_linger(runner):
    """Keep the user's systemd manager alive after the installing session exits."""
    if not shutil.which("loginctl"):
        raise ValueError("systemd user manager is unavailable")
    user = pwd.getpwuid(os.geteuid()).pw_name
    query = runner(["loginctl", "show-user", user, "--property=Linger", "--value"],
                   check=False, capture_output=True, text=True)
    if query.returncode == 0 and query.stdout.strip() == "yes":
        return
    if not shutil.which("sudo"):
        raise ValueError("enable user lingering with: sudo loginctl enable-linger " + user)
    enabled = runner(["sudo", "-n", "loginctl", "enable-linger", user],
                     check=False, capture_output=True, text=True)
    if enabled.returncode != 0:
        raise ValueError("enable user lingering with: sudo loginctl enable-linger " + user)
    verified = runner(["loginctl", "show-user", user, "--property=Linger", "--value"],
                      check=False, capture_output=True, text=True)
    if verified.returncode != 0 or verified.stdout.strip() != "yes":
        raise ValueError("enable user lingering with: sudo loginctl enable-linger " + user)


def install(config, *, home=None, python=sys.executable, module_root=None, runner=subprocess.run):
    config = normalize(config, home=home)
    if not shutil.which("systemctl"):
        raise ValueError("systemd user manager is unavailable")
    _ensure_linger(runner)
    _private_directory(config["state_directory"], create=True)
    base = Path(home or Path.home())
    unit_directory = _private_directory(base / ".config/systemd/user", create=True)
    unit_path = unit_directory / ("atlas-kubekit-observer-" + config["name"] + ".service")
    failure_path = unit_directory / ("atlas-kubekit-observer-failure-" + config["name"] + ".service")
    previous = None
    previous_failure = None
    active = _systemctl(runner, "is-active", unit_path.name)
    enabled = _systemctl(runner, "is-enabled", unit_path.name)
    written_failure = False
    written_main = False
    try:
        previous_failure = _write_owned(
            failure_path, render_failure_unit(config, python=python, module_root=module_root), _FAILURE_MARKER)
        written_failure = True
        previous = _write_owned(unit_path, render_unit(config, python=python, module_root=module_root))
        written_main = True
        if not _systemctl(runner, "daemon-reload") or not _systemctl(runner, "enable", "--now", unit_path.name):
            raise ValueError("systemd could not start the observer service")
    except BaseException:
        if written_main:
            _restore_unit(unit_path, previous, _MARKER)
        if written_failure:
            _restore_unit(failure_path, previous_failure, _FAILURE_MARKER)
        if written_main or written_failure:
            _systemctl(runner, "daemon-reload")
            if enabled:
                _systemctl(runner, "enable", unit_path.name)
                if active:
                    _systemctl(runner, "start", unit_path.name)
        raise
    return {"unit": str(unit_path), "failure_unit": str(failure_path),
            "state_directory": config["state_directory"], "name": config["name"]}


def remove(name, *, home=None, runner=subprocess.run):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid observer name")
    base = Path(home or Path.home())
    unit_directory = _private_directory(base / ".config/systemd/user")
    unit_path = unit_directory / ("atlas-kubekit-observer-" + name + ".service")
    failure_path = unit_directory / ("atlas-kubekit-observer-failure-" + name + ".service")
    _verify_owned(unit_path, _MARKER)
    failure_exists = os.path.lexists(failure_path)
    if failure_exists:
        _verify_owned(failure_path, _FAILURE_MARKER)
        if not _systemctl(runner, "stop", failure_path.name):
            raise ValueError("systemd could not stop the observer failure notification unit")
    if not _systemctl(runner, "disable", "--now", unit_path.name):
        raise ValueError("systemd could not stop the observer service")
    unit_path.unlink()
    if failure_exists:
        failure_path.unlink()
    if not _systemctl(runner, "daemon-reload"):
        raise ValueError("systemd could not reload user units")
    return {"name": name, "state_preserved": True, "failure_unit_removed": True}


def _verify_owned(path, marker):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError("managed observer unit is missing") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) & 0o077 or marker not in path.read_text()):
        raise ValueError("refusing to remove a foreign or non-private observer unit")
