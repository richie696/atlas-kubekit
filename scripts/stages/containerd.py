#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Align CRI sandbox image with kubeadm before a fresh node starts workloads."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

SECTIONS = {
    "v1": ("io.containerd.grpc.v1.cri", "sandbox_image"),
    "v2": ("io.containerd.cri.v1.images", "sandbox"),
}


class ConfigurationError(ValueError):
    pass


def section(line):
    for kind, (plugin, field) in SECTIONS.items():
        tail = r"\.pinned_images" if kind == "v2" else ""
        if re.fullmatch(
            r'\s*\[\s*plugins\.(?:"'
            + re.escape(plugin)
            + r'"|\''
            + re.escape(plugin)
            + r"\')"
            + tail
            + r"\s*\]\s*(?:#.*)?",
            line,
        ):
            return kind
    return None


def projection(text):
    active = None
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            active = section(line)
        if active:
            field = SECTIONS[active][1]
            match = re.fullmatch(r"\s*" + field + r'\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?', line)
            if match:
                found.append((active, match[1]))
    if len(found) != 1:
        raise ConfigurationError("ops.containerd.invalid")
    return found[0]


def candidate(text, kind, image):
    if kind not in SECTIONS or not re.fullmatch(r"registry\.k8s\.io/pause:[A-Za-z0-9._-]+", image):
        raise ConfigurationError("ops.containerd.invalid")
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if section(line.rstrip()) == kind]
    if len(starts) > 1:
        raise ConfigurationError("ops.containerd.invalid")
    field = SECTIONS[kind][1]
    assignment = "  " + field + " = " + json.dumps(image) + "\n"
    if not starts:
        plugin = SECTIONS[kind][0]
        tail = ".pinned_images" if kind == "v2" else ""
        return text.rstrip() + "\n\n[plugins." + json.dumps(plugin) + tail + "]\n" + assignment
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines)
    )
    matches = [i for i in range(start + 1, end) if re.match(r"\s*" + field + r"\s*=", lines[i])]
    if len(matches) > 1:
        raise ConfigurationError("ops.containerd.invalid")
    if matches:
        lines[matches[0]] = assignment
    else:
        lines.insert(start + 1, assignment)
    return "".join(lines)


def run(arguments, check=True):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            arguments,
            stdout=output,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=90,
        )
        if check and result.returncode:
            raise ConfigurationError("ops.containerd.failed")
        if output.tell() > 1024 * 1024:
            raise ConfigurationError("ops.containerd.failed")
        output.seek(0)
        return output.read().decode()


def desired(version, runner=run):
    if not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ConfigurationError("ops.containerd.invalid")
    lines = runner(
        ["kubeadm", "config", "images", "list", "--kubernetes-version", "v" + version.lstrip("v")]
    ).splitlines()
    images = [
        line for line in lines if re.fullmatch(r"registry\.k8s\.io/pause:[A-Za-z0-9._-]+", line)
    ]
    if len(images) != 1:
        raise ConfigurationError("ops.containerd.invalid")
    return images[0]


def align(image, config=Path("/etc/containerd/config.toml"), runner=run, check=False):
    config = Path(config)
    if any(path.is_symlink() for path in (config, *config.parents)):
        raise ConfigurationError("ops.containerd.invalid")
    info = config.stat()
    parent = config.parent.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or info.st_size > 1024 * 1024
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise ConfigurationError("ops.containerd.invalid")
    before = config.read_bytes()
    kind, current = projection(runner(["containerd", "--config", str(config), "config", "dump"]))
    if current == image:
        return False
    if check:
        raise ConfigurationError("ops.containerd.drift")
    with tempfile.NamedTemporaryFile(
        dir=config.parent, prefix=".atlas-sandbox-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(candidate(before.decode(), kind, image).encode())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if projection(runner(["containerd", "--config", str(temporary), "config", "dump"])) != (
            kind,
            image,
        ):
            raise ConfigurationError("ops.containerd.invalid")
        if config.read_bytes() != before:
            raise ConfigurationError("ops.containerd.invalid")
        backup = config.with_name(config.name + ".atlas-before-sandbox-" + uuid.uuid4().hex)
        with os.fdopen(
            os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
        ) as saved:
            saved.write(before)
            saved.flush()
            os.fsync(saved.fileno())
        temporary.chmod(stat.S_IMODE(info.st_mode))
        os.replace(temporary, config)
        try:
            runner(["systemctl", "restart", "containerd"])
            runner(["systemctl", "is-active", "--quiet", "containerd"])
        except BaseException:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                "wb",
            ) as restored:
                restored.write(before)
                restored.flush()
                os.fsync(restored.fileno())
            temporary.chmod(stat.S_IMODE(info.st_mode))
            os.replace(temporary, config)
            runner(["systemctl", "restart", "containerd"], check=False)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return True


def main(argv=None):
    library = Path(__file__).resolve().parent.parent / "lib"
    if not (library / "i18n.py").is_file():
        library = Path(__file__).resolve().parent / "scripts/lib"
    sys.path.insert(0, str(library))
    from i18n import translate

    tr = lambda key, *values: translate(os.environ.get("ATLAS_LANG", "en"), key, *values)
    try:
        if os.geteuid() != 0:
            raise ConfigurationError("ops.error.root")
        args = list(sys.argv[1:] if argv is None else argv)
        check = bool(args and args[0] == "--check")
        if check:
            args.pop(0)
        if len(args) != 1:
            raise ConfigurationError("ops.containerd.invalid")
        image = desired(args[0])
        align(image, check=check)
        print(tr("ops.containerd.ready", image))
        return 0
    except (ConfigurationError, OSError, subprocess.TimeoutExpired) as error:
        print(
            tr(str(error) if isinstance(error, ConfigurationError) else "ops.containerd.failed"),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
