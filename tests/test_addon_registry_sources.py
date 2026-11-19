# SPDX-License-Identifier: MIT
import unittest

from scripts.addons.sources import registry_ref, validate


class AddonRegistrySourceTest(unittest.TestCase):
    def test_daocloud_keeps_ghcr_images_on_upstream(self):
        config = validate({"profile": "daocloud"})
        image = "ghcr.io/jkroepke/kube-webhook-certgen:1.11.1"
        self.assertEqual(registry_ref(image, config), image)

    def test_1panel_keeps_ghcr_images_on_upstream(self):
        config = validate({"profile": "1panel"})
        image = "ghcr.io/jkroepke/kube-webhook-certgen:1.11.1"
        self.assertEqual(registry_ref(image, config), image)

    def test_daocloud_still_mirrors_other_supported_registries(self):
        config = validate({"profile": "daocloud"})
        self.assertEqual(
            registry_ref("quay.io/prometheus/prometheus:v3.8.1", config),
            "m.daocloud.io/quay.io/prometheus/prometheus:v3.8.1",
        )


if __name__ == "__main__":
    unittest.main()
