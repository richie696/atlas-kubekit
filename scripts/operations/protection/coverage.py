# SPDX-License-Identifier: MIT
"""Current identities versus declarations; registration is never restore proof."""
from . import records


def collect(client):
    registered = records.list_records(client)
    declarations = {}
    for record in registered:
        for ref in record["declaration"]["objects"]:
            key = (ref["kind"], ref["namespace"], ref["name"], ref["uid"])
            declarations.setdefault(key, []).append(record["id"])
    current = {}
    names = {key[:3] for key in declarations}
    for kind in ("persistentvolumeclaim", "deployment", "statefulset"):
        for item in client.get(kind)["items"]:
            meta = item["metadata"]
            key = (kind, meta["namespace"], meta["name"], meta["uid"])
            required = (
                kind == "persistentvolumeclaim"
                or meta.get("annotations", {}).get("atlas-kubekit.io/data-protection") == "required"
            )
            if required or key[:3] in names:
                current[key] = dict(
                    kind=kind,
                    namespace=meta["namespace"],
                    name=meta["name"],
                    uid=meta["uid"],
                    declarationIds=sorted(declarations.get(key, [])),
                    state=(
                        "registered-restore-unverified" if key in declarations else "unregistered"
                    ),
                )
    covered_names = {key[:3] for key, row in current.items() if row["declarationIds"]}
    stale = [
        dict(
            kind=key[0],
            namespace=key[1],
            name=key[2],
            uid=key[3],
            declarationIds=sorted(ids),
            state="superseded-identity" if key[:3] in covered_names else "identity-stale",
        )
        for key, ids in declarations.items()
        if key not in current
    ]
    return dict(
        schemaVersion=1,
        clusterIdentity=client.cluster_identity(),
        records=registered,
        objects=sorted(
            [*current.values(), *stale],
            key=lambda row: (row["kind"], row["namespace"], row["name"], row["uid"]),
        ),
        unregistered=sum(row["state"] == "unregistered" for row in current.values()),
        stale=sum(row["state"] == "identity-stale" for row in stale),
        superseded=sum(row["state"] == "superseded-identity" for row in stale),
        restoration="unverified",
        applicationDiscovery="explicit-workload-annotation-or-declaration",
    )
