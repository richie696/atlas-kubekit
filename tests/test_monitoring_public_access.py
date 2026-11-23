# SPDX-License-Identifier: MIT
import unittest
import io
import json
from email.message import Message
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.addons import monitoring_public_access as access


class MonitoringPublicAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.private_ca_bundle = Path(self.temp_directory.name) / "private-ca.pem"
        self.private_ca_bundle.write_text("test-ca")

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_direct_cli_render_works_outside_repository_directory(self):
        script = Path(access.__file__).resolve()
        with tempfile.TemporaryDirectory() as working_directory:
            result = subprocess.run(
                [sys.executable, str(script), "--render", "--domain", "monitor.example.internal",
                 "--tls-secret", "monitoring-wildcard", "--oidc-issuer",
                 "https://sso.example.internal/realms/ops", "--oidc-client-id", "atlas-monitoring",
                 "--oidc-secret", "atlas-monitoring-oidc"],
                cwd=working_directory, text=True, capture_output=True, check=False, timeout=10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gateway.networking.k8s.io/v1", result.stdout)
        self.assertIn("https://grafana.monitor.example.internal/oauth2/callback", result.stdout)

    def values(self):
        return access.resources("monitoring", "eg", "monitor.example.com", "monitoring-wildcard",
                                "https://sso.example.com/realms/ops", "atlas-monitoring",
                                "atlas-monitoring-oidc")

    def test_builds_tls_gateway_routes_and_shared_oidc_policy(self):
        documents = self.values()
        gateway, grafana, prometheus, alertmanager, policy = documents
        self.assertEqual((gateway["kind"], gateway["spec"]["listeners"][0]["protocol"]),
                         ("Gateway", "HTTPS"))
        listener = gateway["spec"]["listeners"][0]
        self.assertEqual(listener["hostname"], "*.monitor.example.com")
        self.assertEqual(listener["tls"]["certificateRefs"][0]["name"], "monitoring-wildcard")
        self.assertEqual([grafana["spec"]["hostnames"][0], prometheus["spec"]["hostnames"][0],
                          alertmanager["spec"]["hostnames"][0]], [
                              "grafana.monitor.example.com", "prometheus.monitor.example.com",
                              "alertmanager.monitor.example.com"])
        self.assertEqual([item["spec"]["rules"][0]["backendRefs"][0]["port"]
                          for item in (grafana, prometheus, alertmanager)], [80, 9090, 9093])
        oidc = policy["spec"]["oidc"]
        self.assertEqual(oidc["provider"]["issuer"], "https://sso.example.com/realms/ops")
        self.assertEqual(oidc["clientSecret"], {"name": "atlas-monitoring-oidc"})
        self.assertEqual(oidc["redirectURL"],
                         "https://grafana.monitor.example.com/oauth2/callback")
        self.assertEqual(oidc["cookieDomain"], "monitor.example.com")

    def test_grafana_html_navigation_renews_partial_oidc_session(self):
        _, _, _, _, policy = self.values()
        route = self.values()[1]
        healthy, refresh, hmac, default = route["spec"]["rules"]
        suffix = policy["spec"]["oidc"]["cookieNames"]["suffix"]
        self.assertEqual(healthy["backendRefs"], default["backendRefs"])
        for rule, cookie in ((healthy, "IdToken"), (refresh, "RefreshToken"), (hmac, "OauthHMAC")):
            match = rule["matches"][0]
            self.assertEqual(match["method"], "GET")
            self.assertEqual(match["headers"][0]["name"], "Accept")
            self.assertIn(cookie + "-" + suffix + "=", match["headers"][1]["value"])
        for rule in (refresh, hmac):
            headers, redirect = rule["filters"]
            self.assertIn("Max-Age=0", headers["responseHeaderModifier"]["set"][0]["value"])
            self.assertEqual(redirect["requestRedirect"], {"scheme": "https", "statusCode": 302})
        for other in self.values()[2:4]:
            self.assertEqual(len(other["spec"]["rules"]), 1)

    def test_manifest_contains_secret_reference_not_credential_value(self):
        rendered = access.render_yaml(self.values())
        self.assertIn("atlas-monitoring-oidc", rendered)
        self.assertNotIn("client-secret-value", rendered)
        self.assertIn("apiVersion: gateway.envoyproxy.io/v1alpha1", rendered)

    def test_internal_mode_uses_nodeport_and_keeps_tls_termination_on_gateway(self):
        documents = access.resources(
            "monitoring", "eg", "monitoring.home.arpa", "atlas-monitoring-tls",
            "https://sso.home.arpa/realms/ops", "atlas-monitoring", "atlas-monitoring-oidc",
            "internal", envoy_proxy_image=(
                "m.daocloud.io/docker.io/envoyproxy/envoy:distroless-v1.40.2"
                "@sha256:09ca6f2140a61ad8df11779a4a30d52b48433d444466328494f9188c9c35eded"))
        proxy, gateway = documents[:2]
        self.assertEqual(proxy["kind"], "EnvoyProxy")
        envoy_service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
        self.assertEqual(envoy_service,
                         {"type": "NodePort", "externalTrafficPolicy": "Cluster"})
        self.assertEqual(proxy["spec"]["provider"]["kubernetes"]["envoyDeployment"]["container"]["image"],
                         "m.daocloud.io/docker.io/envoyproxy/envoy:distroless-v1.40.2"
                         "@sha256:09ca6f2140a61ad8df11779a4a30d52b48433d444466328494f9188c9c35eded")
        self.assertEqual(gateway["spec"]["infrastructure"]["parametersRef"], {
            "group": "gateway.envoyproxy.io", "kind": "EnvoyProxy",
            "name": "atlas-monitoring-nodeport"})
        self.assertEqual(gateway["spec"]["listeners"][0]["tls"]["mode"], "Terminate")

    def test_private_oidc_ca_enables_only_the_required_envoy_backend_api(self):
        root = Path(__file__).resolve().parents[1]
        values = access.yaml_module().safe_load(
            (root / "config/addons/envoy-gateway-oidc-backend.yaml").read_text())
        self.assertEqual(values, {"config": {"envoyGateway": {
            "extensionApis": {"enableBackend": True}}}})

        script = (root / "03-install-addons.sh").read_text()
        public_access = script[script.index("monitoring_public_access_setup() {"):
                               script.index("\nmonitoring_probe_core_dns() {")]
        self.assertEqual(public_access.count(
            'EXTRA_VALUES+=(-f "$SCRIPT_DIR/config/addons/envoy-gateway-oidc-backend.yaml")'), 2)
        self.assertIn('if [ -n "$oidc_ca_bundle" ]; then', public_access)

    def test_public_access_report_uses_private_user_state_directory(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        self.assertIn('report_dir="${XDG_STATE_HOME:-$HOME/.local/state}/atlas-kubekit/reports"',
                      script)
        self.assertIn('chmod 700 "$report_dir"', script)
        self.assertIn('monitoring-public-access-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")', script)
        self.assertNotIn("report_dir=/var/lib/k8s-deploy/reports", script)

    def test_rejects_invalid_envoy_proxy_image(self):
        with self.assertRaisesRegex(ValueError, "invalid Envoy Proxy"):
            access.validate_envoy_proxy_image("docker.io/envoyproxy/envoy:latest;rm -rf /")

    def test_rejects_non_https_oidc_issuer_and_invalid_host_or_resource_names(self):
        with self.assertRaises(ValueError):
            access.resources("monitoring", "eg", "monitor.example.com", "cert",
                             "http://sso.example.com", "client", "secret")
        with self.assertRaises(ValueError):
            access.resources("monitoring", "eg", "monitor.example.com/path", "cert",
                             "https://sso.example.com", "client", "secret")
        with self.assertRaises(ValueError):
            access.resources("monitoring", "eg", "monitor.example.com", "bad_secret",
                             "https://sso.example.com", "client", "secret")

    def test_rejects_oidc_issuer_credentials_query_and_fragment(self):
        for issuer in ("https://user:pass@sso.example.com", "https://sso.example.com?x=1",
                       "https://sso.example.com/#fragment"):
            with self.subTest(issuer=issuer), self.assertRaises(ValueError):
                access.resources("monitoring", "eg", "monitor.example.com", "cert",
                                 issuer, "client", "secret")

    def test_rejects_localhost_oidc_issuer(self):
        for issuer in ("https://localhost", "https://as.localhost", "https://127.0.0.1"):
            with self.subTest(issuer=issuer), self.assertRaises(ValueError):
                access.validate_oidc_issuer(issuer)

    def test_oidc_discovery_requires_reachable_matching_issuer_and_https_endpoints(self):
        issuer = "https://sso.example.internal/"
        metadata = {"issuer": issuer, "authorization_endpoint": "https://sso.example.internal/authorize",
                    "token_endpoint": "https://sso.example.internal/token",
                    "jwks_uri": "https://sso.example.internal/jwks"}
        with patch.object(access, "_is_private_host", return_value=False), \
                patch.object(access, "urlopen", return_value=io.BytesIO(
                json.dumps(metadata).encode("utf-8"))) as opener:
            self.assertEqual(access.verify_oidc_issuer(issuer), metadata)
        self.assertTrue(opener.called)

        for invalid in (
                {**metadata, "issuer": "https://other.example.internal/"},
                {**metadata, "token_endpoint": "http://sso.example.internal/token"},
                {**metadata, "jwks_uri": "https://keys.example.internal/jwks"},
                {key: value for key, value in metadata.items() if key != "jwks_uri"}):
            with self.subTest(metadata=invalid), patch.object(
                    access, "urlopen", return_value=io.BytesIO(
                        json.dumps(invalid).encode("utf-8"))), \
                    patch.object(access, "_is_private_host", return_value=False), \
                    patch.object(access.ssl, "create_default_context"), \
                    self.assertRaises(RuntimeError):
                access.verify_oidc_issuer(issuer, ca_bundle=str(self.private_ca_bundle))

    def test_private_ca_discovery_allows_only_same_origin_oidc_endpoints(self):
        issuer = "https://sso.example.internal/realms/ops"
        metadata = {"issuer": issuer,
                    "authorization_endpoint": "https://sso.example.internal/auth",
                    "token_endpoint": "https://sso.example.internal/token",
                    "jwks_uri": "https://sso.example.internal/jwks"}
        with patch.object(access, "_is_private_host", return_value=False), \
                patch.object(access, "urlopen", return_value=io.BytesIO(
                json.dumps(metadata).encode("utf-8"))), \
                patch.object(access.ssl, "create_default_context"):
            self.assertEqual(access.verify_oidc_issuer(
                issuer, ca_bundle=str(self.private_ca_bundle)), metadata)
        metadata["token_endpoint"] = "https://oauth-token.example.internal/token"
        with patch.object(access, "_is_private_host", return_value=False), \
                patch.object(access, "urlopen", return_value=io.BytesIO(
                json.dumps(metadata).encode("utf-8"))), \
                patch.object(access.ssl, "create_default_context"), self.assertRaisesRegex(
                    RuntimeError, "privately trusted provider host"):
            access.verify_oidc_issuer(issuer, ca_bundle=str(self.private_ca_bundle))

    def test_private_names_use_direct_connection_without_desktop_proxy(self):
        self.assertTrue(access._is_private_host("oauth.home.arpa"))
        self.assertTrue(access._is_private_host("10.20.1.200"))
        with patch.object(access, "_resolve", return_value=["10.20.1.200"]):
            self.assertTrue(access._is_private_host("oauth.example"))

    def test_wait_succeeds_only_when_gateway_policy_and_all_routes_are_ready(self):
        gateway = {"status": {"conditions": [{"type": "Programmed", "status": "True"}],
                              "addresses": [{"value": "203.0.113.4"}]}}
        policy = {"status": {"conditions": [{"type": "Accepted", "status": "True"}]}}
        route = {"status": {"parents": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}
        def resource(_namespace, kind, name):
            if kind == "gateway":
                return gateway
            if kind == "securitypolicy":
                return policy
            return route
        with patch.object(access, "_kubectl_json", side_effect=resource):
            self.assertEqual(access.verify_resources("monitoring", timeout=1), ["203.0.113.4"])

    def test_route_status_requires_accepted_and_resolved_refs(self):
        route = {"status": {"parents": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "False"}]}]}}
        self.assertFalse(access._route_accepted(route))

    def test_condition_reads_gateway_api_conditions_from_ancestors(self):
        backend_tls_policy = {"status": {"ancestors": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}
        self.assertTrue(access._condition(backend_tls_policy, "Accepted"))
        self.assertTrue(access._condition(backend_tls_policy, "ResolvedRefs"))
        backend_tls_policy["status"]["ancestors"][0]["conditions"][0]["status"] = "False"
        self.assertFalse(access._condition(backend_tls_policy, "Accepted"))

    def test_public_probe_reports_all_component_hostnames(self):
        with patch.object(access, "verify_resources", return_value=["203.0.113.4"]), \
                patch.object(access, "probe_access", return_value=[
                    {"host": "grafana.monitor.example.com", "state": "passed", "reason": "ok"},
                    {"host": "prometheus.monitor.example.com", "state": "passed", "reason": "ok"},
                    {"host": "alertmanager.monitor.example.com", "state": "passed", "reason": "ok"}]), \
                patch("builtins.print") as output:
            self.assertEqual(access.verify_public_access(
                "monitoring", "monitor.example.com", "https://sso.example.com", timeout=1), 0)
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("https://grafana.monitor.example.com", rendered)
        self.assertIn("https://prometheus.monitor.example.com", rendered)
        self.assertIn("https://alertmanager.monitor.example.com", rendered)
        self.assertIn("https://grafana.monitor.example.com/oauth2/callback", rendered)

    def test_failed_gateway_wait_still_reports_addresses_and_returns_failure(self):
        evidence = [{"host": f"{name}.monitor.example.com", "state": "passed", "reason": "ok"}
                    for name in access.COMPONENTS]
        with patch.object(access, "verify_resources", side_effect=RuntimeError("not programmed")), \
                patch.object(access, "probe_access", return_value=evidence), \
                patch("builtins.print") as output:
            self.assertEqual(access.verify_public_access(
                "monitoring", "monitor.example.com", "https://sso.example.com", timeout=1), 1)
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("https://grafana.monitor.example.com", rendered)
        self.assertIn("not programmed", rendered)

    def test_internal_verification_prints_private_gateway_ip(self):
        evidence = [{"host": f"{name}.monitor.example.internal", "state": "passed", "reason": "ok"}
                    for name in access.COMPONENTS]
        with patch.object(access, "verify_resources", return_value=["10.20.1.43"]), \
                patch.object(access, "probe_access", return_value=evidence), \
                patch("builtins.print") as output:
            self.assertEqual(access.verify_public_access(
                "monitoring", "monitor.example.internal", "https://sso.example.com",
                timeout=1, exposure_mode="internal", expected_address="10.20.1.43"), 0)
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("internal-only", rendered)
        self.assertIn("Internal HAProxy ingress VIP", rendered)
        self.assertIn("10.20.1.43", rendered)

    def test_internal_verification_matches_dns_to_haproxy_vip_and_reports_gateway_nodes(self):
        evidence = [{"host": f"{name}.monitoring.home.arpa", "state": "passed", "reason": "ok"}
                    for name in access.COMPONENTS]
        with patch.object(access, "verify_resources", return_value=[
                "10.20.1.34", "10.20.1.35"]), \
                patch.object(access, "probe_access", return_value=evidence) as probe, \
                patch("builtins.print") as output:
            self.assertEqual(access.verify_public_access(
                "monitoring", "monitoring.home.arpa", "https://oauth.home.arpa",
                timeout=1, exposure_mode="internal", expected_address="10.20.1.43"), 0)
        self.assertEqual(set(probe.call_args.kwargs["expected_addresses"]),
                         {"10.20.1.34", "10.20.1.35", "10.20.1.43"})
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("Internal HAProxy ingress VIP: 10.20.1.43", rendered)
        self.assertIn("Envoy Gateway node addresses: 10.20.1.34, 10.20.1.35", rendered)

    def test_internal_resource_verification_accepts_expected_vip_without_gateway_address(self):
        gateway = {"status": {"conditions": [{"type": "Programmed", "status": "True"}]}}
        policy = {"status": {"conditions": [{"type": "Accepted", "status": "True"}]}}
        route = {"status": {"parents": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}
        def resource(_namespace, kind, _name):
            return gateway if kind == "gateway" else policy if kind == "securitypolicy" else route
        with patch.object(access, "_kubectl_json", side_effect=resource):
            self.assertEqual(access.verify_resources(
                "monitoring", timeout=1, expected_address="10.20.1.43"), ["10.20.1.43"])

    def test_internal_resource_verification_requires_accepted_oidc_backend_tls(self):
        gateway = {"status": {"conditions": [{"type": "Programmed", "status": "True"}],
                              "addresses": [{"value": "10.20.1.43"}]}}
        policy = {"status": {"conditions": [{"type": "Accepted", "status": "True"}]}}
        route = {"status": {"parents": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}
        tls_policy = {"status": {"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}}

        def resource(_namespace, kind, _name):
            if kind == "gateway": return gateway
            if kind == "securitypolicy": return policy
            if kind == "backend": return {"kind": "Backend"}
            if kind == "backendtlspolicy": return tls_policy
            return route

        with patch.object(access, "_kubectl_json", side_effect=resource):
            self.assertEqual(access.verify_resources(
                "monitoring", timeout=1, expected_address="10.20.1.43",
                require_oidc_backend=True), ["10.20.1.43"])

    def test_internal_resource_verification_accepts_backend_tls_ancestor_conditions(self):
        gateway = {"status": {"conditions": [{"type": "Programmed", "status": "True"}],
                              "addresses": [{"value": "10.20.1.34"}]}}
        policy = {"status": {"conditions": [{"type": "Accepted", "status": "True"}]}}
        route = {"status": {"parents": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}
        tls_policy = {"status": {"ancestors": [{"conditions": [
            {"type": "Accepted", "status": "True"},
            {"type": "ResolvedRefs", "status": "True"}]}]}}

        def resource(_namespace, kind, _name):
            if kind == "gateway": return gateway
            if kind == "securitypolicy": return policy
            if kind == "backend": return {"kind": "Backend"}
            if kind == "backendtlspolicy": return tls_policy
            return route

        with patch.object(access, "_kubectl_json", side_effect=resource):
            self.assertEqual(access.verify_resources(
                "monitoring", timeout=1, expected_address="10.20.1.43",
                require_oidc_backend=True), ["10.20.1.34"])

    def test_internal_mode_accepts_private_dns_pointing_to_gateway_and_oidc_redirect(self):
        headers = Message()
        headers["Location"] = "https://sso.example.com/authorize"

        def oidc_redirect(request, timeout):
            raise HTTPError(request.full_url, 302, "Found", headers, None)

        with patch.object(access, "_resolve", return_value=["10.20.1.40"]), \
                patch.object(access, "build_opener") as opener:
            opener.return_value.open.side_effect = oidc_redirect
            evidence = access.probe_access(
                "monitor.example.internal", "https://sso.example.com/realms/ops",
                exposure_mode="internal", expected_addresses=["10.20.1.40"])

        self.assertEqual([item["state"] for item in evidence], ["passed"] * 4)
        self.assertEqual([item["reason"] for item in evidence],
                         ["https-and-oidc-redirect"] * 4)

    def test_internal_mode_rejects_public_or_mismatched_dns_before_https_probe(self):
        with patch.object(access, "_resolve", return_value=["10.20.1.41"]), \
                patch.object(access, "build_opener") as opener:
            evidence = access.probe_access(
                "monitor.example.internal", "https://sso.example.com/realms/ops",
                exposure_mode="internal", expected_addresses=["10.20.1.40"])

        self.assertEqual([item["reason"] for item in evidence],
                         ["dns-does-not-match-gateway"] * 4)
        opener.return_value.open.assert_not_called()

    def test_https_probe_can_use_explicit_internal_ca_bundle(self):
        headers = Message()
        headers["Location"] = "https://sso.example.com/authorize"

        def oidc_redirect(request, timeout):
            raise HTTPError(request.full_url, 302, "Found", headers, None)

        with patch.object(access, "_resolve", return_value=["10.20.1.43"]), \
                patch.object(access.ssl, "create_default_context") as create_context, \
                patch.object(access, "build_opener") as opener:
            opener.return_value.open.side_effect = oidc_redirect
            evidence = access.probe_access(
                "monitoring.home.arpa", "https://sso.example.com/realms/ops",
                exposure_mode="internal", expected_addresses=["10.20.1.43"],
                ca_bundle="/tmp/atlas-ca.pem")

        create_context.assert_called_once_with(cafile="/tmp/atlas-ca.pem")
        self.assertEqual([item["state"] for item in evidence], ["passed"] * 4)


if __name__ == "__main__":
    unittest.main()
