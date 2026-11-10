"""Version policy survives distribution and overrides inherited install defaults."""

import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from scripts.lib.component_versions import catalog, pinned
from scripts.operations.adapters.host import bundle

ROOT = Path(__file__).resolve().parents[1]
from tests.shell import BASH


class VersionTests(unittest.TestCase):
    def test_native_stage_uses_catalog_instead_of_exported_component_versions(self):
        program = """set -euo pipefail
source "$1/scripts/lib/versions.sh"
atlas_versions_load "$1/scripts/lib"
source "$1/scripts/deploy/stages.sh"
atlas_deploy_lock_fence() { :; }
q() { printf '%q' "$1"; }
run() { printf '%s' "$2"; }
ATLAS_LANG=en LIB=/usr/local/lib/k8s-deploy VIP=192.0.2.9 API_PORT=6443
POD_CIDR=10.244.0.0/16 SVC_CIDR=10.96.0.0/12 VRRP_ID=51 TEST_MODE=1
declare -A NODE_IFACE=([cp1]=eth0) IP=([cp1]=192.0.2.11)
atlas_deploy_stage cp1 setup-k8s-node.sh DATA_DEVICE=none 192.0.2.11 cp1 cp --k8s-only --yes
"""
        output = subprocess.run(
            [BASH, "-c", program, "test", str(ROOT)],
            env={**os.environ, "K8S_VERSION": "9.9.9", "CILIUM_VERSION": "9.9.9"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        arguments = shlex.split(output)
        self.assertIn("K8S_VERSION=" + pinned()["kubernetes"][1:], arguments)
        self.assertIn("CILIUM_VERSION=" + pinned()["cilium"], arguments)
        self.assertNotIn("K8S_VERSION=9.9.9", arguments)
        self.assertEqual(arguments[-5:], ["192.0.2.11", "cp1", "cp", "--k8s-only", "--yes"])

    def test_zip_runtime_and_extracted_native_assets_read_the_same_catalog(self):
        program = """import json,subprocess,sys
sys.path.insert(0,sys.argv[1])
from scripts.lib.component_versions import catalog
from scripts.operations.host_actions.stage_assets import installed
with installed() as root:
 result=subprocess.run([sys.executable,str(root/'lib/component_versions.py')],capture_output=True,text=True,check=True)
 print(json.dumps(dict(catalog=catalog(),native=result.stdout)))
"""
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "runtime.zip"
            file.write_bytes(base64.b64decode(bundle()))
            output = subprocess.run(
                [sys.executable, "-I", "-c", program, str(file)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        result = json.loads(output)
        self.assertEqual(result["catalog"], catalog())
        self.assertIn("ATLAS_KUBERNETES_VERSION\t" + pinned()["kubernetes"], result["native"])


if __name__ == "__main__":
    unittest.main()
