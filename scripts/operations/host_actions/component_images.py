# SPDX-License-Identifier: MIT
"""Warm digest-pinned CRI images before any Cilium templates or Pods change."""
import re

from ..collector import identity_facts
from ..models import OperationError
from . import common

CRI = ["crictl", "--runtime-endpoint=unix:///run/containerd/containerd.sock"]


def prepare(value, run_id):
    if (
        not isinstance(value, dict)
        or set(value) != {"images"}
        or not isinstance(value["images"], list)
        or not 1 <= len(value["images"]) <= 16
    ):
        raise OperationError("ops.change.invalid_parameters")
    images = value["images"]
    if any(
        not isinstance(ref, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,1023}@sha256:[a-f0-9]{64}", ref)
        for ref in images
    ):
        raise OperationError("ops.component.image_override")
    if len(images) != len(set(images)):
        raise OperationError("ops.change.invalid_parameters")
    for reference in images:
        status, _ = common.run([*CRI, "inspecti", reference], check=False, timeout=30)
        if status:
            common.run([*CRI, "pull", reference], timeout=300)
        common.run([*CRI, "inspecti", reference], timeout=30)
    return dict(identity=identity_facts(), images=sorted(images))
