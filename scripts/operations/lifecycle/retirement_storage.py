# SPDX-License-Identifier: MIT
"""Retirement must prove that node-bound storage has been evacuated."""
import time

from ..models import OperationError
from .rules import selector_matches


def matches_term(term, node):
    labels = node["metadata"].get("labels", {})
    if set(term) - {"matchExpressions", "matchFields"}:
        raise OperationError("ops.lifecycle.local_data")
    if not term.get("matchExpressions") and not term.get("matchFields"):
        return False
    fields = {"metadata.name": node["metadata"]["name"]}
    return selector_matches(
        {"matchExpressions": term.get("matchExpressions", [])}, labels
    ) and selector_matches({"matchExpressions": term.get("matchFields", [])}, fields)


def require_evacuated(client, node, peers=()):
    volumes = []
    for pv in client.get("persistentvolume")["items"]:
        spec = pv.get("spec", {})
        local=any(key in spec for key in ('local','hostPath'))
        terms = spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms")
        on_target=not terms or any(matches_term(term,node) for term in terms)
        if local and on_target:
            raise OperationError("ops.lifecycle.local_data")
        if not local:
            if terms and on_target and not any(matches_term(term,peer) for peer in peers
                    if peer['metadata']['name']!=node['metadata']['name'] for term in terms):
                raise OperationError('ops.lifecycle.local_data')
            continue
        volumes.append(dict(uid=pv["metadata"]["uid"], name=pv["metadata"]["name"]))
    resources = client.call(
        ["api-resources", "--api-group=longhorn.io", "-o", "name"], json_output=False
    )
    if "replicas.longhorn.io" in resources.splitlines():
        replicas = client.get("replicas.longhorn.io")["items"]
        if any(item.get("spec", {}).get("nodeID") == node["metadata"]["name"] for item in replicas):
            raise OperationError("ops.lifecycle.storage_evacuation")
    return dict(localVolumesOnOtherNodes=sorted(volumes, key=lambda row: row["name"]))


def wait_detached(client, name, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        attached = [
            row
            for row in client.get("volumeattachment")["items"]
            if row.get("spec", {}).get("nodeName") == name
        ]
        if not attached:
            return
        if time.monotonic() >= deadline:
            raise OperationError("ops.lifecycle.storage_evacuation")
        time.sleep(1)
