# SPDX-License-Identifier: MIT
"""Build a native Alertmanager email receiver without embedding credentials.

Only versions with a verified SMTP implementation are accepted. The caller is
responsible for mounting the referenced secret and merging without overwriting
existing Alertmanager routes or receivers.
"""
import re
from pathlib import PurePosixPath

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z")
_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
_MAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,253}\Z")
_VERSIONS = {"0.28.1", "0.31.1", "0.34.2", "0.35.0"}


def _mail(value):
    if not isinstance(value, str) or not _MAIL.fullmatch(value):
        raise ValueError("invalid email address")
    if not _HOST.fullmatch(value.rsplit("@", 1)[1]):
        raise ValueError("invalid email address")
    return value


def receiver(name, version, host, port, sender, recipient, username, password_file):
    if (not isinstance(name, str) or not _NAME.fullmatch(name) or version not in _VERSIONS or
            not isinstance(host, str) or not _HOST.fullmatch(host) or
            type(port) is not int or port not in (465, 587)):
        raise ValueError("invalid or unsupported email configuration")
    if (not isinstance(password_file, str) or not password_file.startswith("/") or
            ".." in PurePosixPath(password_file).parts or "{{" in password_file or
            any(ord(ch) < 32 for ch in password_file) or len(password_file) > 1024):
        raise ValueError("invalid credential reference")
    # v0.28.1 tries STARTTLS even on an already implicit-TLS socket when
    # require_tls=true. Port 465 is hard-coded to TLS in that version's client.
    require_tls = port != 465 or version != "0.28.1"
    config = {"to": _mail(recipient), "from": _mail(sender), "smarthost": f"{host}:{port}",
              "auth_username": _mail(username), "auth_password_file": password_file,
              "send_resolved": True, "require_tls": require_tls}
    return {"name": name, "email_configs": [config]}
