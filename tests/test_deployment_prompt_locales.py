"""Keep deployment-critical registry guidance visible in both locales."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
from scripts.lib import i18n


REPO = Path(__file__).resolve().parents[1]


class DeploymentPromptLocaleTests(unittest.TestCase):
    def test_translation_keys_outside_messages_are_rejected(self):
        catalog = json.loads((REPO / "locales/en.json").read_text())
        catalog["ops.misplaced.message"] = "unreachable text"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "en.json").write_text(json.dumps(catalog))
            with patch.object(i18n, "LOCALES_DIR", path):
                with self.assertRaises(i18n.LocalizationError):
                    i18n.validate_catalogs()

    def test_local_registry_prompt_requires_stable_node_reachable_endpoint(self):
        english = json.loads((REPO / "locales/en.json").read_text())
        chinese = json.loads((REPO / "locales/zh_cn.json").read_text())

        en_prompt = english["messages"]["phase02.prompt.local_registry_host"]
        zh_prompt = chinese["messages"]["phase02.prompt.local_registry_host"]

        self.assertIn("DHCP/IPAM-reserved address", en_prompt)
        self.assertIn("stable DNS name", en_prompt)
        self.assertIn("every Kubernetes node", en_prompt)
        self.assertIn("DHCP/IPAM 保留", zh_prompt)
        self.assertIn("稳定 DNS 名称", zh_prompt)
        self.assertIn("每个 K8s 节点", zh_prompt)
        self.assertNotIn("10.20.1.157", en_prompt + zh_prompt)


if __name__ == "__main__":
    unittest.main()
