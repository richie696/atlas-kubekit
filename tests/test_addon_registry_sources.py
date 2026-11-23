# SPDX-License-Identifier: MIT
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.addons.sources import registry_ref, resolve_envoy_proxy, validate


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

    def test_envoy_proxy_image_uses_selected_registry_mirror_and_keeps_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "chart.yaml"
            defaults = root / "values.yaml"
            previous = root / "existing.yaml"
            generated = root / "envoy-proxy.yaml"
            metadata.write_text("name: gateway-helm\nversion: 1.10.0\nappVersion: 1.10.0\n")
            defaults.write_text("{}\n")
            previous.write_text("{}\n")
            image = ("docker.io/envoyproxy/envoy:distroless-v1.40.2"
                     "@sha256:09ca6f2140a61ad8df11779a4a30d52b48433d444466328494f9188c9c35eded")
            with patch("scripts.addons.sources.version_proxy_image",
                       return_value=(image, "pinned release", [])):
                resolve_envoy_proxy(str(metadata), str(generated),
                                    [str(defaults), str(previous)], validate({"profile": "daocloud"}))
            values = generated.read_text()
            self.assertIn("m.daocloud.io/docker.io/envoyproxy/envoy:distroless-v1.40.2", values)
            self.assertIn("@sha256:09ca6f2140a61ad8df11779a4a30d52b48433d444466328494f9188c9c35eded", values)
            self.assertIn("m.daocloud.io/docker.io/envoyproxy/envoy", json.loads(
                Path(str(generated) + ".json").read_text())["image"])


if __name__ == "__main__":
    unittest.main()
