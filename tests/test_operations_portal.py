# SPDX-License-Identifier: MIT
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/addons"))
from monitoring_portal import IMAGE, install, manifests
from portal.render import files, registered_links


class OperationsPortalTest(unittest.TestCase):
    def test_links_follow_domain_and_discovered_dashboard_uid(self):
        configmaps = [{"data": {"board.json": '{"uid":"dns-board","title":"DNS <test>"}',
                                "invalid.json": "oops", "notes": "message"}}]
        page = files("ops.example.com", configmaps, [])["index.html"]
        self.assertIn('href="https://grafana.ops.example.com/d/dns-board">DNS &lt;test&gt;', page)
        self.assertIn('href="https://prometheus.ops.example.com/targets"', page)
        self.assertIn('href="https://alertmanager.ops.example.com/#/silences"', page)
        self.assertNotIn("home.arpa", page)
        self.assertGreater(page.count("<a "), 4)
        self.assertEqual(page.count("<a "), page.count('<a target="_blank" rel="noopener noreferrer"'))

    def test_only_registered_tools_are_published_and_escaped(self):
        entry = {"group": "生产 <A>", "name": "卷管理", "url": "https://storage.example.com/"}
        page = files("ops.example.com", [], [entry])["index.html"]
        self.assertIn('生产 &lt;A&gt;', page)
        self.assertIn('href="https://storage.example.com/"', page)
        self.assertNotIn("storage.example.com", files("ops.example.com", [], [])["index.html"])
        for url in ("http://a.example/", "javascript:alert(1)", "https://a.example/?token=x",
                    "https://user:pass@a.example/", "https://a.example\\@b.example/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                registered_links([{**entry, "url": url}])

    def test_dedicated_route_does_not_capture_grafana_or_forward_credentials(self):
        config, workload, service, route, legacy = manifests("ops.example.com", {"index.html": "test"}, [], IMAGE)
        self.assertEqual(route["spec"]["hostnames"], ["ops.ops.example.com"])
        rule, = route["spec"]["rules"]
        self.assertEqual(rule["matches"], [{"path": {"type": "PathPrefix", "value": "/"}}])
        self.assertEqual(legacy["spec"]["hostnames"], ["grafana.ops.example.com"])
        old_rule, = legacy["spec"]["rules"]
        self.assertEqual(old_rule["matches"][0]["path"]["value"], "/ops")
        self.assertEqual(old_rule["filters"][0]["requestRedirect"]["hostname"], "ops.ops.example.com")
        self.assertEqual(set(rule["filters"][0]["requestHeaderModifier"]["remove"]),
                         {"X-Atlas-ID-Token", "Authorization", "Cookie"})
        self.assertEqual(rule["backendRefs"][0]["name"], service["metadata"]["name"])
        pod = workload["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], config["metadata"]["name"])

    @patch("monitoring_portal.subprocess.run")
    @patch("monitoring_portal.get", return_value={})
    def test_missing_oidc_stops_before_mutation(self, _get, run):
        with self.assertRaisesRegex(ValueError, "OIDC"):
            install("ops.example.com", None, None)
        run.assert_not_called()

    @patch("monitoring_portal.require_gateway")
    @patch("monitoring_portal.subprocess.run")
    @patch("monitoring_portal.get", return_value={"metadata": {"labels": {}}})
    def test_foreign_resource_stops_before_mutation(self, _get, run, _gateway):
        with self.assertRaisesRegex(ValueError, "占用"):
            install("ops.example.com", None, None)
        run.assert_not_called()
