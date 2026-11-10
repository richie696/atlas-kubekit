"""Registration proves byte integrity, never content coverage or application restore."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts.operations.protection import schema, age_archive, registration, records, coverage
from scripts.operations.models import OperationError


class ProtectionTests(unittest.TestCase):
    def declaration(
        self, path="/private/backup.age", raw=b"age-encryption.org/v1\nCIPHERTEXT-DATA"
    ):
        return dict(
            schemaVersion=1,
            provider="age-archive",
            clusterIdentity="cluster-test-uid",
            createdAt="2026-01-01T00:00:00+00:00",
            objects=[
                dict(
                    kind="persistentvolumeclaim",
                    namespace="app",
                    name="database",
                    uid="original-pvc-uid",
                )
            ],
            artifact=dict(path=str(path), size=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
        )

    def test_credential_and_command_fields_or_duplicate_objects_are_rejected(self):
        for field in ("password", "command", "identity"):
            value = self.declaration()
            value[field] = "PRIVATE"
            with self.assertRaises(OperationError):
                schema.normalize(value)
        value = self.declaration()
        value["objects"] *= 2
        with self.assertRaises(OperationError):
            schema.normalize(value)
        public = schema.public(self.declaration())
        self.assertNotIn("/private/backup.age", json.dumps(public))
        self.assertEqual(schema.validate_public(public), public)

    def test_byte_tampering_and_public_file_permissions_fail_without_restore_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory).resolve() / "payload.age"
            raw = b"age-encryption.org/v1\nCIPHERTEXT-DATA"
            p.write_bytes(raw)
            p.chmod(0o600)
            value = self.declaration(p, raw)
            evidence = age_archive.verify(value["artifact"])
            self.assertEqual(evidence["decryption"], "unverified")
            self.assertEqual(evidence["restoration"], "unverified")
            p.write_bytes(raw[:-1] + b"X")
            with self.assertRaises(OperationError):
                age_archive.verify(value["artifact"])
            p.write_bytes(raw)
            p.chmod(0o644)
            with self.assertRaises(OperationError):
                age_archive.verify(value["artifact"])

    def test_record_publish_is_idempotent_and_refuses_foreign_records(self):
        client = Mock()
        client.cluster_identity.return_value = "cluster-test-uid"
        client.get.return_value = None
        value = self.declaration()
        record = dict(
            schemaVersion=1,
            id=schema.record_id(value),
            declaration=schema.public(value),
            evidence=dict(
                integrity="ciphertext-verified", decryption="unverified", restoration="unverified"
            ),
        )
        client.create.return_value = {}
        self.assertTrue(records.publish(client, record))
        stored = client.create.call_args.args[0]
        client.get.return_value = stored
        self.assertFalse(records.publish(client, record))
        self.assertEqual(client.create.call_count, 1)
        stored["metadata"]["labels"] = {}
        with self.assertRaises(OperationError):
            records.publish(client, record)
        self.assertEqual(client.create.call_count, 1)

    def test_recreated_pvc_has_unregistered_new_identity_and_stale_old_evidence(self):
        value = self.declaration()
        record = dict(
            schemaVersion=1,
            id=schema.record_id(value),
            declaration=schema.public(value),
            evidence=dict(
                integrity="ciphertext-verified", decryption="unverified", restoration="unverified"
            ),
        )
        from scripts.operations.changes.coordinator import managed_map

        stored = managed_map(
            records.PREFIX + record["id"][:24], records.NAMESPACE, dict(record=json.dumps(record))
        )
        history = [stored]
        client = Mock()
        client.cluster_identity.return_value = "cluster-test-uid"

        def get(kind, **kwargs):
            if kind == "configmap":
                return dict(items=history)
            if kind == "persistentvolumeclaim":
                return dict(
                    items=[
                        dict(metadata=dict(name="database", namespace="app", uid="rebuilt-pvc-uid"))
                    ]
                )
            return dict(items=[])

        client.get.side_effect = get
        result = coverage.collect(client)
        self.assertEqual(result["unregistered"], 1)
        self.assertEqual(result["stale"], 1)
        self.assertEqual(result["restoration"], "unverified")
        newer = copy.deepcopy(value)
        newer["objects"][0]["uid"] = "rebuilt-pvc-uid"
        second = dict(record, id=schema.record_id(newer), declaration=schema.public(newer))
        history.append(
            managed_map(
                records.PREFIX + second["id"][:24],
                records.NAMESPACE,
                dict(record=json.dumps(second)),
            )
        )
        refreshed = coverage.collect(client)
        self.assertEqual(refreshed["stale"], 0)
        self.assertEqual(refreshed["unregistered"], 0)
        self.assertEqual(refreshed["superseded"], 1)
        self.assertEqual(len(refreshed["records"]), 2)


if __name__ == "__main__":
    unittest.main()
