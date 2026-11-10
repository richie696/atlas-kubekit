# SPDX-License-Identifier: MIT
"""Do not label an APT closure signed when source options bypass authentication."""
from email.parser import Parser
from pathlib import Path
import re
import shlex

from ..host_actions import common
from ..models import OperationError

STRICT_OPTIONS = (
    "APT::Get::AllowUnauthenticated=false",
    "Acquire::AllowInsecureRepositories=false",
    "Acquire::AllowWeakRepositories=false",
    "Acquire::AllowDowngradeToInsecureRepositories=false",
    "Acquire::https::Verify-Peer=true",
    "Acquire::https::Verify-Host=true",
)


def denied():
    raise OperationError("ops.materials.apt_security")


def check_source(text, deb822=False):
    dangerous = ("trusted", "allow-insecure", "allow-weak", "allow-downgrade-to-insecure")
    if deb822:
        for stanza in re.split(r"\n\s*\n", text):
            fields = Parser().parsestr(stanza)
            if fields.get("Enabled", "yes").strip().casefold() == "no":
                continue
            if any(
                fields.get(name, "").strip().casefold() in ("yes", "true", "1")
                for name in dangerous
            ):
                denied()
    else:
        for line in text.splitlines():
            content = line.split("#", 1)[0].strip()
            if not re.match(r"^deb(?:-src)?\s", content):
                continue
            if re.search(
                r"\b(?:" + "|".join(dangerous) + r")\s*=\s*(?:yes|true|1)\b", content, re.I
            ):
                denied()


def check_transport(config):
    if re.search(
        r'^\s*Acquire::https::(?:[^\s"]+::)?Verify-(?:Peer|Host)\s+"(?:false|0|no)"',
        config,
        re.I | re.M,
    ):
        denied()


def require_configured():
    _, config = common.run(["apt-config", "dump"])
    check_transport(config)
    _, raw = common.run(
        [
            "apt-config",
            "shell",
            "ATLAS_SOURCE",
            "Dir::Etc::sourcelist/f",
            "ATLAS_PARTS",
            "Dir::Etc::sourceparts/d",
        ]
    )
    settings = dict(item.split("=", 1) for item in shlex.split(raw))
    if set(settings) != {"ATLAS_SOURCE", "ATLAS_PARTS"}:
        denied()
    main, parts = Path(settings["ATLAS_SOURCE"]), Path(settings["ATLAS_PARTS"])
    files = [main] if main.is_file() else []
    if parts.is_dir():
        files += sorted(path for path in parts.iterdir() if path.suffix in (".list", ".sources"))
    for path in files:
        check_source(common.read_owned(path, 2 * 1024 * 1024).decode(), path.suffix == ".sources")
