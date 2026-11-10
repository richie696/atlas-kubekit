# SPDX-License-Identifier: MIT
"""Plan a declaration, verify bytes and object identities, then register public evidence."""
from datetime import datetime, timezone
from ..configuration import normalize
from ..models import OperationError
from . import schema, age_archive, records


def from_arguments(args, configuration, environment):
    if not args.evidence_file:
        raise OperationError("ops.protection.invalid")
    return dict(configuration=configuration, declaration=schema.load(args.evidence_file))


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "declaration"}:
        raise OperationError("ops.change.invalid_parameters")
    return dict(
        configuration=normalize(value["configuration"]),
        declaration=schema.normalize(value["declaration"]),
    )


def preview(values):
    declaration = values["declaration"]
    return (
        (
            "ops.preview.protection",
            (len(declaration["objects"]), declaration["artifact"]["sha256"]),
        ),
    )


def object_facts(client, declaration):
    rows = []
    for ref in declaration["objects"]:
        actual = client.get(ref["kind"], ref["name"], ref["namespace"], optional=True)
        if actual is None or actual["metadata"]["uid"] != ref["uid"]:
            raise OperationError("ops.protection.identity")
        rows.append(dict(ref))
    return rows


def observe(value, environment):
    values = parameters(value)
    environment.verify(values["configuration"])
    declaration = values["declaration"]
    if declaration["clusterIdentity"] != environment.client.cluster_identity():
        raise OperationError("ops.change.wrong_cluster")
    if datetime.fromisoformat(declaration["createdAt"]) > datetime.now(timezone.utc):
        raise OperationError("ops.protection.invalid")
    evidence = age_archive.verify(declaration["artifact"])
    objects = object_facts(environment.client, declaration)
    identifier = schema.record_id(declaration)
    existing = environment.client.get(
        "configmap", records.PREFIX + identifier[:24], records.NAMESPACE, optional=True
    )
    old = records.read(existing, declaration["clusterIdentity"]) if existing is not None else None
    return dict(objects=objects, evidence=evidence, existing=old)


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    record = dict(
        schemaVersion=1,
        id=schema.record_id(values["declaration"]),
        declaration=schema.public(values["declaration"]),
        evidence=facts["evidence"],
    )
    checkpoint("register-data-protection")
    created = records.publish(environment.client, record)
    return dict(
        recordId=record["id"],
        created=created,
        declaredObjects=len(facts["objects"]),
        integrity="ciphertext-verified",
        decryption="unverified",
        restoration="unverified",
    )
