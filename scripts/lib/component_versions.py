# SPDX-License-Identifier: MIT
"""One version catalog for repository, installed runtime and SSH zip packages."""
import importlib.resources
import json
from pathlib import Path
import re
import sys

NAME = "component-versions.json"
FIELDS = {
    "kubernetes": "ATLAS_KUBERNETES_VERSION",
    "cilium": "ATLAS_CILIUM_VERSION",
    "helm": "ATLAS_HELM_VERSION",
    "criTools": "ATLAS_CRI_TOOLS_VERSION",
}


def catalog():
    text = (
        importlib.resources.files(__package__).joinpath(NAME).read_text()
        if __package__
        else Path(__file__).with_name(NAME).read_text()
    )

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate component catalog field")
            value[key] = item
        return value

    value = json.loads(text, object_pairs_hook=unique)
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", *FIELDS}
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
    ):
        raise ValueError("Invalid component version catalog")
    for field in FIELDS:
        prefix = "" if field == "cilium" else "v"
        if not isinstance(value[field], str) or not re.fullmatch(
            prefix + r"[0-9]+\.[0-9]+\.[0-9]+", value[field]
        ):
            raise ValueError("Invalid component version catalog")
    return value


def pinned():
    value = catalog()
    return {field: value[field] for field in ("kubernetes", "cilium")}


def main():
    try:
        value = catalog()
        for field, variable in FIELDS.items():
            print(variable + "\t" + value[field])
        return 0
    except (OSError, ValueError):
        print("Invalid or missing component version catalog", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
