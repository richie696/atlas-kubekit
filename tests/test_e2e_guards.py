"""A lab driver must fail before opening a remote installation channel."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.e2e import transport, offline


class LabGuardTests(unittest.TestCase):
    def test_pretrusted_peers_do_not_request_an_inapplicable_enrollment_code(self):
        nodes = (SimpleNamespace(name='lb1', address='172.29.230.31'), SimpleNamespace(name='cp1', address='172.29.230.33'))
        fixture = SimpleNamespace(nodes=nodes, material_id='a' * 64)
        calls = []
        def run(lab, node, text):
            calls.append(text)
            if text.endswith('runtime.py id'):return fixture.material_id
            if text.startswith('ssh '):return 'cp1'
            if text=='hostname -s':return node.name
            return ''
        with tempfile.TemporaryDirectory() as temporary, patch.object(transport, 'guard'), patch.object(transport, 'run', side_effect=run), patch.object(transport, 'interactive', return_value=(0, ['confirmed'])):
            result = offline.deployment(fixture, Path(temporary))
        self.assertEqual(result['paired'], [])
        self.assertFalse(any('--code' in command or '--enable-enrollment' in command for command in calls))

    def test_disconnect_before_confirmation_is_not_accepted_as_reboot(self):
        fixture = SimpleNamespace(public_key="ssh-ed25519 test")
        node = SimpleNamespace(name="cp1", interface="enp6s18", address="172.29.230.33")
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(transport, "guard"),
            patch.object(transport, "interactive", return_value=(255, ["phase01.prompt.hostname"])),
        ):
            with self.assertRaises(RuntimeError):
                offline.phase_one(fixture, node, Path(temporary))

    def test_foreign_lab_marker_is_refused_before_other_commands(self):
        fixture = SimpleNamespace(id="atlas-e2e-owned")
        node = SimpleNamespace(name="cp1", address="172.29.230.33", vmid=223)
        value = dict(schemaVersion=1, labId="atlas-e2e-foreign", node="cp1", vmid=223)
        with patch.object(transport, "run", return_value=json.dumps(value)) as run:
            with self.assertRaises(RuntimeError):
                transport.guard(fixture, node)
            self.assertEqual(run.call_count, 1)

    def test_existing_log_prevents_starting_a_remote_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "private.log"
            log.write_text("preserve")
            with patch.object(transport.subprocess, "Popen") as process:
                with self.assertRaises(FileExistsError):
                    transport.interactive(None, None, "never execute", [], log, 1)
                process.assert_not_called()
            self.assertEqual(log.read_text(), "preserve")
