"""Old nodes upgrade with fixed bundled helpers, independent of installed files."""

import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations.adapters.host import bundle
from scripts.operations.host_actions import upgrade
from scripts.operations.models import OperationError


class UpgradeRuntimeTests(unittest.TestCase):
    def test_active_material_blocks_before_package_commands_or_node_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "materials.json"
            state.write_text("{}")
            with (
                patch.object(upgrade, "MATERIAL_STATE", state),
                patch.object(upgrade.common, "run") as run,
            ):
                with self.assertRaises(OperationError) as error:
                    upgrade.observe(dict(version="v1.36.6"))
                self.assertEqual(error.exception.key, "ops.materials.invalid")
                run.assert_not_called()
            state.unlink()
            state.symlink_to(Path(temporary) / "missing")
            with (
                patch.object(upgrade, "MATERIAL_STATE", state),
                patch.object(upgrade.common, "run") as run,
            ):
                with self.assertRaises(OperationError):
                    upgrade.observe(dict(version="v1.36.6"))
                run.assert_not_called()

    def test_package_action_loads_real_assets_from_the_distributed_zip(self):
        program = r"""
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from scripts.operations.host_actions import upgrade
observed=[]
def run(args, log, timeout):
    assets=Path(args[4])
    assert assets.is_dir() and '/usr/local/lib/k8s-deploy' not in args[2]
    assert 'source "$1/lib/packages.sh"' in args[2]
    assert args[5:]==['install','--simulate','kubeadm=1.36.6-1.1']
    assert timeout==900
    for name in ('lib/packages.sh','lib/i18n.sh','lib/i18n.py','locales/en.json'):
        assert (assets/name).is_file() and (assets/name).stat().st_mode & 0o777==0o600
    observed.append(assets)
upgrade.private_process.run=run
upgrade.apt(['install','--simulate','kubeadm=1.36.6-1.1'],Path(sys.argv[2]))
assert len(observed)==1 and not observed[0].exists()
print('packaged-helper-verified')
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "runtime.zip"
            archive.write_bytes(base64.b64decode(bundle()))
            result = subprocess.run(
                [sys.executable, "-c", program, str(archive), str(root / "apt.log")],
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertEqual(result.stdout.strip(), "packaged-helper-verified")


if __name__ == "__main__":
    unittest.main()
