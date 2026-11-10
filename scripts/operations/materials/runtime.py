#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Native deployment access to an already verified, root-owned offline bundle."""
import json
import os
from pathlib import Path
import sys
import re

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.lib.i18n import translate
from scripts.operations.host_actions import common, private_process
from scripts.operations.models import OperationError
from scripts.operations.materials import schema, images
from scripts.operations.materials.installer import DESTINATION, STATE, owned_directory


def active():
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")
    value = json.loads(common.read_owned(STATE))
    if (
        set(value) != {"schemaVersion", "id", "root"}
        or value["schemaVersion"] != 1
        or not isinstance(value["id"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["id"])
        or str(DESTINATION / value["id"]) != value["root"]
    ):
        raise OperationError("ops.materials.invalid")
    base = owned_directory(value["root"], 0o755, create=False)
    manifest = schema.read(base)
    if manifest["id"] != value["id"]:
        raise OperationError("ops.error.integrity")
    return base, manifest


def selected(kind, name=None):
    base, manifest = active()
    rows = [
        item
        for item in manifest["artifacts"]
        if item["kind"] == kind and (name is None or item["name"] == name)
    ]
    if not rows:
        raise OperationError("ops.materials.incomplete")
    for item in rows:
        file = base / item["path"]
        if (
            any(path.is_symlink() for path in (file, *file.parents))
            or not file.is_file()
            or file.stat().st_uid != 0
            or file.stat().st_mode & 0o022
            or file.stat().st_size != item["size"]
            or schema.digest(file) != item["sha256"]
        ):
            raise OperationError("ops.error.integrity")
    return base, rows


def import_images():
    base, manifest = active()
    logdir = owned_directory(STATE.parent / "material-logs", 0o700)
    _, raw = common.run(["ctr", "-n", "k8s.io", "images", "list"])
    existing = {parts[0]: parts[2] for row in raw.splitlines() if len(parts := row.split()) >= 3}
    for item in manifest["artifacts"]:
        if item["kind"] != "image":
            continue
        expected = item["metadata"].get("digest")
        cached = images.cached_reference(item["name"])
        if existing.get(cached) == expected:
            continue
        file = base / item["path"]
        if schema.digest(file) != item["sha256"]:
            raise OperationError("ops.error.integrity")
        images.verify(file, expected)
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".image-", dir=logdir) as temporary:
            private_process.run(
                ["ctr", "-n", "k8s.io", "images", "import", "--platform", "linux/amd64", str(file)],
                Path(temporary) / "import.log",
                timeout=600,
            )
        _, actual = common.run(["ctr", "-n", "k8s.io", "images", "list", "name==" + cached])
        matches = [
            row.split() for row in actual.splitlines() if row.split() and row.split()[0] == cached
        ]
        if len(matches) != 1 or matches[0][2] != expected:
            raise OperationError("ops.error.integrity")


def apt(args):
    base, manifest = active()
    for kind in ("apt-index", "apt-signature", "apt-key"):
        selected(kind)
    if "full-upgrade" in args:
        print(translate(os.environ.get("ATLAS_LANG", "en"), "ops.materials.os_upgrade"))
        return 0
    source = STATE.parent / "materials.list"
    common.replace(
        source,
        (
            "deb [signed-by="
            + str(base / "apt/repository.gpg")
            + "] file:"
            + str(base / "apt")
            + " ./\n"
        ).encode(),
        0o644,
    )
    os.execvp(
        "apt-get",
        [
            "apt-get",
            "-o",
            "Dir::Etc::sourcelist=" + str(source),
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "Acquire::AllowInsecureRepositories=false",
            "-o",
            "APT::Get::AllowUnauthenticated=false",
            *args,
        ],
    )


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if not args:
            raise OperationError("ops.materials.invalid")
        if args[0] == "apt":
            return apt(args[1:])
        if args == ["images-import"]:
            import_images()
            return 0
        if args == ["id"]:
            print(active()[1]["id"])
            return 0
        if len(args) == 3 and args[0] == "check-versions":
            versions = active()[1]["configuration"]["versions"]
            if args[1] != "v" + versions["kubernetes"].lstrip("v") or args[2] != versions["cilium"]:
                raise OperationError("ops.materials.invalid")
            return 0
        if args in (["helm-path"], ["chart-path"]):
            base, rows = (
                selected("tool", "helm") if args[0] == "helm-path" else selected("chart", "cilium")
            )
            if len(rows) != 1:
                raise OperationError("ops.materials.incomplete")
            print(base / rows[0]["path"])
            return 0
        raise OperationError("ops.materials.invalid")
    except (OperationError, OSError, ValueError, KeyError, TypeError) as error:
        key = error.key if isinstance(error, OperationError) else "ops.materials.invalid"
        print(translate(os.environ.get("ATLAS_LANG", "en"), key), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
