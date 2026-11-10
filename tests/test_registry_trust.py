"""Public CA handling and optional intent stay independent of private credentials."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import configuration
from scripts.operations.registry_ca import public_ca
from scripts.operations.capabilities.registry_configure import certificate
from scripts.operations.host_actions import registry
from scripts.operations.models import OperationError


class RegistryTests(unittest.TestCase):
    def test_optional_registry_preserves_existing_revision_and_rejects_secret_fields(self):
        old=configuration.normalize(configuration.template());before=configuration.revision(old)
        self.assertNotIn('registry',configuration.normalize(old))
        self.assertEqual(configuration.revision(configuration.normalize(old)),before)
        updated=copy.deepcopy(old);updated['registry']=dict(authority='REGISTRY.example:5443',caFingerprint='a'*64)
        normalized=configuration.normalize(updated)
        self.assertEqual(normalized['registry']['authority'],'registry.example:5443')
        self.assertNotEqual(configuration.revision(normalized),before)
        updated['registry']['password']='must-not-be-stored'
        with self.assertRaises(OperationError):configuration.normalize(updated)
        with self.assertRaises(OperationError):configuration.deployment_rows(normalized)

    def test_private_key_files_are_rejected_before_distribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            file=Path(temporary).resolve()/'combined.pem'
            raw=b'-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n'
            file.write_bytes(raw);file.chmod(0o600)
            with self.assertRaises(OperationError):certificate(str(file))
            with self.assertRaises(OperationError):public_ca(raw)
            self.assertEqual(public_ca(b''),b'')

    def test_foreign_registry_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary).resolve();config=base/'config.toml';config.write_text('config_path="/etc/containerd/certs.d"');config.chmod(0o600)
            host='registry.example:5443';directory=base/(host.replace(':','_')+'_');directory.mkdir()
            file=directory/'hosts.toml';file.write_text('server="https://custom.example"');file.chmod(0o600)
            with patch.object(registry,'DIRECTORY',base),patch.object(registry,'CONFIG',config),\
                    patch.object(registry.common,'run',return_value=(0,'config_path = "/etc/containerd/certs.d"')),\
                    patch.object(registry,'endpoint') as endpoint:
                with self.assertRaises(OperationError):registry.observe(dict(authority=host,certificate=''))
            endpoint.assert_not_called()
            self.assertEqual(file.read_text(),'server="https://custom.example"')


if __name__=='__main__':unittest.main()
