"""Isolated maintenance contracts; no SSH, sudo, live source or real restore."""

from contextlib import nullcontext
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import shutil
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from scripts.operations import backup, models, reporting, transport
from scripts.operations import cli


class RulesTests(unittest.TestCase):
    def facts(self):
        return dict(
            role="lb",
            bootstrap=dict(state="ok", broad=True),
            pairing=dict(state="ok", active=False),
            ssh=dict(
                state="ok",
                settings=dict(
                    passwordauthentication="no",
                    kbdinteractiveauthentication="no",
                    permitrootlogin="no",
                ),
            ),
            stats=dict(state="ok", listeners=["127.0.0.1:7000"]),
            services=dict(haproxy="active", keepalived="active"),
        )

    def test_rules_are_pure_and_cleanup_remains_review_only(self):
        facts = self.facts()
        before = copy.deepcopy(facts)
        result = models.security_findings(facts)
        plan = models.cleanup_plan(facts)
        self.assertEqual(before, facts)
        self.assertEqual(models.exit_status(result), 2)
        self.assertIn("review-bootstrap-sudo", plan.actions)
        self.assertFalse(any("delete" in a or "remove" in a for a in plan.actions))

    def test_unknown_is_not_a_pass(self):
        findings = models.security_findings({})
        self.assertTrue(all(f.status == "UNVERIFIED" for f in findings[:-1]))
        self.assertEqual(models.exit_status(findings), 2)

    def test_exposed_stats_and_service_failure_are_distinct(self):
        facts = self.facts()
        facts["stats"]["listeners"] = ["*:7000"]
        self.assertEqual(
            next(f.status for f in models.security_findings(facts) if f.check == "lb-stats"), "WARN"
        )
        facts["services"]["haproxy"] = "inactive"
        self.assertEqual(models.exit_status(models.service_findings(facts)), 1)

    def test_fingerprints_are_order_independent_but_change_with_facts(self):
        self.assertEqual(models.fingerprint({"x": 1, "y": 2}), models.fingerprint({"y": 2, "x": 1}))
        self.assertNotEqual(models.fingerprint({"x": 1}), models.fingerprint({"x": 2}))

    def test_non_cp_cannot_plan_a_control_plane_snapshot(self):
        with self.assertRaises(models.OperationError):
            backup.backup_plan(self.facts(), "control-plane", ())

    def test_transport_rejects_command_injection_before_ssh(self):
        with patch.object(subprocess, "run") as run:
            with self.assertRaises(models.OperationError):
                transport.node_facts("ubuntu@host;id")
            run.assert_not_called()


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir="/private/tmp" if Path("/private/tmp").is_dir() else None
        )
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class PrivateFilesTests(FixtureCase):
    def test_nested_private_creation_does_not_leave_public_intermediate_directories(self):
        root = self.base / "new-output"
        reporting.private_directory(root / "plans" / "nested")
        for directory in (root, root / "plans", root / "plans" / "nested"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(reporting.private_directory(root), root)

    def test_shared_and_symlink_output_directories_are_rejected(self):
        public = self.base / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(models.OperationError):
            reporting.private_directory(public)
        link = self.base / "link"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(models.OperationError):
            reporting.private_directory(link / "reports")

    def test_report_is_private_and_does_not_copy_unknown_facts(self):
        root = self.base / "reports"
        directory, data = reporting.write_report(
            root,
            run_id="run-1",
            version="dev",
            created_at="now",
            target="test",
            findings=(models.Finding("x", "UNVERIFIED", "sample.key"),),
            translate=lambda k, *values: k,
            plan=models.cleanup_plan({"private": "do-not-copy"}),
        )
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for file in directory.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("do-not-copy", (directory / "report.json").read_text())
        self.assertEqual(json.loads((directory / "report.json").read_text())["runId"], "run-1")
        with self.assertRaises(FileExistsError):
            reporting.write_report(
                root,
                run_id="run-1",
                version="dev",
                created_at="now",
                target="test",
                findings=(),
                translate=lambda k, *values: k,
            )

    def test_inventory_rejects_source_symlinks_and_overlap(self):
        source = self.base / "source"
        source.mkdir()
        (source / "config").write_text("private")
        relative = source.relative_to("/").as_posix()
        with patch.object(backup, "SOURCES", (relative,)):
            with self.assertRaises(models.OperationError):
                backup.source_inventory(source / "backup")
            (source / "link").symlink_to("/etc/hosts")
            with self.assertRaises(models.OperationError):
                backup.source_inventory(self.base / "out")

    def test_restore_plan_never_creates_destination_or_accepts_live_paths(self):
        manifest = {"scope": "configuration"}
        destination = self.base / "isolated"
        plan = backup.restore_plan(manifest, {}, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(plan.operation, "restore-review")
        for target in ("/etc/kubernetes/restore", "/var/lib/etcd/restore", "/", "/run/new"):
            with self.subTest(target=target), self.assertRaises(models.OperationError):
                backup.restore_plan(manifest, {}, target)


class BackupVerificationTests(FixtureCase):
    def fixture(self, *, corrupt_hash=False, extra=None, duplicate=False, coverage=None):
        directory = self.base / "backup"
        directory.mkdir(mode=0o700)
        cipher = directory / "payload.tar.age"
        cipher.write_bytes(b"encrypted-fixture")
        cipher.chmod(0o600)
        identity = self.base / "identity"
        identity.write_text("fictional-key")
        identity.chmod(0o600)
        content = b"fixture: only configuration"
        manifest = dict(
            schemaVersion=1,
            runId="fixture",
            sourceFingerprint="fixture-source",
            scope="configuration",
            coverage=coverage
            or dict(configuration=True, etcd=False, volumes=False, applications=False),
            files=[
                dict(
                    path="etc/hosts",
                    size=len(content),
                    sha256="bad" if corrupt_hash else hashlib.sha256(content).hexdigest(),
                )
            ],
        )
        archive = self.base / "clear.tar"
        with tarfile.open(archive, "w") as stream:

            def add(name, value):
                member = tarfile.TarInfo(name)
                member.size = len(value)
                stream.addfile(member, io.BytesIO(value))

            add("payload/etc/hosts", content)
            if duplicate:
                add("payload/etc/hosts", content)
            if extra:
                add(extra, b"other")
            add("manifest.json", json.dumps(manifest).encode())
        envelope = {k: v for k, v in manifest.items() if k != "files"}
        envelope["payload"] = dict(
            name=cipher.name, size=cipher.stat().st_size, sha256=backup.digest_file(cipher)
        )
        reporting.atomic_json(directory / "backup.json", envelope)
        return directory, identity, archive

    def verify(self, directory, identity, archive):
        def fake_age(args, **kwargs):
            Path(args[args.index("--output") + 1]).write_bytes(archive.read_bytes())
            return ""

        with patch.object(backup, "require_root"), patch.object(backup, "run_tool", fake_age):
            return backup.verify_backup(directory, identity)

    def test_valid_manifest_and_private_staging_cleanup(self):
        directory, identity, archive = self.fixture()
        result = self.verify(directory, identity, archive)
        self.assertEqual(result["scope"], "configuration")
        self.assertFalse(any(p.name.startswith(".verify-") for p in directory.iterdir()))

    def test_corrupt_per_file_hash_is_rejected(self):
        with self.assertRaises(models.OperationError):
            self.verify(*self.fixture(corrupt_hash=True))

    def test_ciphertext_tampering_is_rejected_before_decryption(self):
        directory, identity, archive = self.fixture()
        (directory / "payload.tar.age").write_bytes(b"tampered")
        with patch.object(backup, "run_tool") as run, patch.object(backup, "require_root"):
            with self.assertRaises(models.OperationError):
                backup.verify_backup(directory, identity)
            run.assert_not_called()

    def test_extra_archive_paths_and_duplicates_are_rejected(self):
        for extra, duplicate in [
            ("payload/../../outside", False),
            ("unlisted-file", False),
            (None, True),
        ]:
            with self.subTest(extra=extra, duplicate=duplicate):
                with tempfile.TemporaryDirectory(dir=self.base) as temp:
                    before = self.base
                    self.base = Path(temp)
                    try:
                        with self.assertRaises(models.OperationError):
                            self.verify(*self.fixture(extra=extra, duplicate=duplicate))
                    finally:
                        self.base = before

    def test_claimed_application_coverage_is_rejected(self):
        with self.assertRaises(models.OperationError):
            self.verify(
                *self.fixture(
                    coverage=dict(configuration=True, etcd=False, volumes=True, applications=True)
                )
            )

    def test_unsafe_identity_permissions_are_rejected(self):
        directory, identity, archive = self.fixture()
        identity.chmod(0o644)
        with self.assertRaises(models.OperationError):
            self.verify(directory, identity, archive)


class BackupCreationTests(FixtureCase):
    def create(self, fail=False):
        source = self.base / "source"
        source.mkdir()
        (source / "settings").write_text("fixture-private-value")
        output = self.base / "backups"

        def age(args, **kwargs):
            if fail:
                raise models.OperationError("ops.error.tool_failed", "age")
            shutil.copyfile(args[-1], args[args.index("--output") + 1])
            return ""

        with (
            patch.object(backup, "SOURCES", (source.relative_to("/").as_posix(),)),
            patch.object(backup, "require_root"),
            patch.object(backup, "operation_lock", return_value=nullcontext()),
            patch.object(backup.shutil, "which", return_value="/fixture/age"),
            patch.object(backup, "run_tool", age),
        ):
            result = backup.create_backup(
                output,
                run_id="fixture",
                version="dev",
                created_at="now",
                facts=dict(identityFingerprint="node", role="lb"),
                scope="configuration",
                recipient="age1" + "q" * 58,
            )
        return result, output

    def test_archive_publication_is_atomic_and_private(self):
        (directory, plan), output = self.create()
        self.assertEqual(plan.operation, "backup-create")
        self.assertEqual({p.name for p in directory.iterdir()}, {"backup.json", "payload.tar.age"})
        self.assertFalse(list(output.glob(".partial-*")))
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture-private-value", (directory / "backup.json").read_text())

    def test_encryption_failure_does_not_publish_backup(self):
        with self.assertRaises(models.OperationError):
            self.create(fail=True)
        self.assertFalse(list((self.base / "backups").iterdir()))

    def test_conflicting_mutation_lock_is_rejected_and_released(self):
        with (
            patch.object(backup, "require_root"),
            patch.object(backup, "LOCK_DIRECTORY", self.base / "locks"),
            patch.object(backup, "reservation", return_value=None),
        ):
            with backup.operation_lock():
                with self.assertRaises(models.OperationError) as error:
                    with backup.operation_lock():
                        pass
                self.assertEqual(error.exception.key, "ops.error.locked")
            with backup.operation_lock():
                pass

    def test_backup_refuses_a_persistently_reserved_lb_transaction(self):
        with (
            patch.object(backup, "require_root"),
            patch.object(backup, "LOCK_DIRECTORY", self.base / "locks"),
            patch.object(backup, "reservation", return_value="a" * 32),
        ):
            with self.assertRaises(models.OperationError) as error:
                with backup.operation_lock():
                    self.fail("partial LB configuration must not be backed up")
            self.assertEqual(error.exception.key, "ops.host.reserved")


class CliTests(FixtureCase):
    def test_failed_collection_saves_a_failed_report_without_raw_error(self):
        output = self.base / "reports"
        with patch.object(
            cli.inspection, "node_facts", side_effect=OSError("private-error-content")
        ):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = cli.main(["--lang", "en", "--report", "--output-dir", str(output)])
        self.assertEqual(result, 1)
        reports = list(output.glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        value = json.loads(reports[0].read_text())
        self.assertEqual(value["result"], "failed")
        self.assertNotIn("private-error-content", reports[0].read_text())

    def test_remote_backup_is_refused_before_ssh(self):
        with patch.object(cli.inspection, "node_facts") as collect:
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = cli.main(
                    [
                        "--backup-plan",
                        "--target",
                        "ubuntu@host",
                        "--output-dir",
                        str(self.base / "reports"),
                    ]
                )
            self.assertEqual(result, 1)
            collect.assert_not_called()

    def test_backup_needs_confirmation_before_any_backup_effect(self):
        facts = dict(hostname="cp1", role="cp")
        with (
            patch.object(cli.inspection, "node_facts", return_value=facts),
            patch.object(backup, "require_root"),
            patch.object(backup, "create_backup") as create,
        ):
            with (
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
                patch("sys.stdin.isatty", return_value=False),
            ):
                result = cli.main(["--backup-create", "--output-dir", str(self.base / "reports")])
            self.assertEqual(result, 1)
            create.assert_not_called()

    def test_restore_plan_reports_not_executed_and_does_not_create_target(self):
        target = self.base / "target"
        out = self.base / "reports"
        with (
            patch.object(
                cli.inspection, "node_facts", return_value=dict(hostname="cp1", role="cp")
            ),
            patch.object(backup, "require_root"),
            patch.object(backup, "verify_backup", return_value=dict(scope="configuration")),
        ):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = cli.main(
                    [
                        "--restore-plan",
                        "--backup",
                        "fixture",
                        "--identity",
                        "fixture",
                        "--restore-target",
                        str(target),
                        "--output-dir",
                        str(out),
                    ]
                )
        self.assertEqual(result, 2)
        self.assertFalse(target.exists())
        data = json.loads(next(out.glob("*/report.json")).read_text())
        self.assertEqual(data["planExecution"], "not-executed")
        self.assertIn(["isolatedDestination", str(target)], data["plan"]["parameters"])

    def test_three_language_help_uses_external_messages(self):
        for locale in ("en", "zh_CN", "zh_TW"):
            with self.subTest(locale=locale), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exit:
                    cli.main(["--lang", locale, "--help"])
                self.assertEqual(exit.exception.code, 0)
                self.assertIn(cli.translate(locale, "ops.cli.backup_verify"), output.getvalue())


if __name__ == "__main__":
    unittest.main()
