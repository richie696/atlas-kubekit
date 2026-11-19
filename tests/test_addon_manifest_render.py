# SPDX-License-Identifier: MIT
import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.addons import sources


class AddonManifestRenderTest(unittest.TestCase):
    def test_plain_equals_enum_round_trips_as_a_string(self):
        yaml = sources.yaml_module()
        manifest = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: test.example.io
spec:
  names:
    kind: Test
  versions:
    - name: v1
      schema:
        openAPIV3Schema:
          type: object
          properties:
            matchType:
              type: string
              enum:
                - '!='
                - =
                - =~
"""
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(manifest)), redirect_stdout(output):
            sources.render(sources.validate({"profile": "official"}), "en")

        rendered = list(yaml.safe_load_all(output.getvalue()))
        enum = rendered[0]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        self.assertEqual(enum["properties"]["matchType"]["enum"], ["!=", "=", "=~"])


if __name__ == "__main__":
    unittest.main()
