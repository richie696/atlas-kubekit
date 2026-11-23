# SPDX-License-Identifier: MIT
"""Render and verify the public OIDC-protected monitoring Gateway."""
import hashlib
import argparse
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request,
                            build_opener, urlopen)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.addons.sources import yaml_module
from scripts.addons.monitoring_oidc_trust import resources as oidc_trust_resources


OWNER = "atlas-kubekit.io/managed-by"
PUBLIC_ACCESS = "atlas-kubekit.io/monitoring-public-access"
COMPONENTS = {
    "grafana": ("kube-prometheus-stack-grafana", 80),
    "prometheus": ("kube-prometheus-stack-prometheus", 9090),
    "alertmanager": ("kube-prometheus-stack-alertmanager", 9093),
}
UI_HOSTS = (*COMPONENTS, "ops")
RESOURCE_NAMES = {
    "gateway": "atlas-monitoring-public",
    "policy": "atlas-monitoring-oidc",
    **{f"route_{name}": f"atlas-monitoring-{name}" for name in COMPONENTS},
}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_K8S_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_CONTAINER_IMAGE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?/)?"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*"
    r":[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@sha256:[a-fA-F0-9]{64})?$")


def validate_kubernetes_name(value, field):
    if not isinstance(value, str) or not _K8S_NAME.fullmatch(value) or len(value) > 63:
        raise ValueError(f"invalid {field}")
    return value


def validate_monitoring_domain(value):
    if (not isinstance(value, str) or len(value) > 242 or value.endswith(".")
            or "." not in value or ":" in value):
        raise ValueError("invalid monitoring base domain")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("monitoring base domain must be a DNS name")
    labels = value.split(".")
    if len(labels) < 2 or any(not _DNS_LABEL.fullmatch(label) or len(label) > 63 for label in labels):
        raise ValueError("invalid monitoring base domain")
    return value


def validate_oidc_issuer(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("invalid OIDC issuer")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("OIDC issuer must be HTTPS without credentials, query, or fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("OIDC issuer must not use localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        raise ValueError("OIDC issuer must not use a loopback address")
    return value


def verify_oidc_issuer(issuer, timeout=8, ca_bundle=None):
    """Verify OIDC discovery reachability and metadata consistency before applying resources."""
    issuer = validate_oidc_issuer(issuer)
    if timeout < 1 or timeout > 60:
        raise ValueError("OIDC discovery timeout must be from 1 to 60 seconds")
    if ca_bundle and (not Path(ca_bundle).is_file() or not os.access(ca_bundle, os.R_OK)):
        raise ValueError("OIDC CA bundle is not a readable file")

    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    request = Request(discovery_url, headers={"Accept": "application/json"})
    hostname = urlsplit(issuer).hostname
    try:
        if _is_private_host(hostname):
            response = build_opener(ProxyHandler({}), HTTPSHandler(context=context)).open(
                request, timeout=timeout)
        else:
            response = urlopen(request, timeout=timeout, context=context)
        with response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("OIDC discovery document is too large")
        metadata = json.loads(payload)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        raise RuntimeError("OIDC discovery is unreachable or invalid") from None

    if not isinstance(metadata, dict) or metadata.get("issuer") != issuer:
        raise RuntimeError("OIDC discovery issuer does not exactly match the configured issuer")
    for endpoint in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = metadata.get(endpoint)
        if not isinstance(value, str):
            raise RuntimeError("OIDC discovery is missing a required endpoint")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("OIDC discovery contains a non-HTTPS endpoint")
        if ca_bundle and (parsed.hostname.rstrip(".").lower() !=
                          urlsplit(issuer).hostname.rstrip(".").lower()
                          or parsed.port not in (None, 443)):
            raise RuntimeError("OIDC endpoints do not share the privately trusted provider host")
    return metadata


def validate_oidc_client_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid OIDC client ID")
    return value


def validate_envoy_proxy_image(value):
    if not isinstance(value, str) or not _CONTAINER_IMAGE.fullmatch(value):
        raise ValueError("invalid Envoy Proxy container image")
    return value


def grafana_session_rules(backend, domain, suffix):
    """Renew access-only browser sessions before Grafana falls back to local login."""
    def cookie_match(name):
        return [{"method": "GET", "headers": [
            {"type": "RegularExpression", "name": "Accept", "value": "(?i).*text/html.*"},
            {"type": "RegularExpression", "name": "Cookie",
                              "value": r"(^|.*;[ \t]*)" + name + "-" + suffix + r"=[^;]+(;.*|$)"}]}]

    rules = [{**backend, "matches": cookie_match("IdToken")}]
    for name in ("RefreshToken", "OauthHMAC"):
        # Clear one cookie per redirect. Gateway API models Set-Cookie as one
        # header; separate responses preserve native browser cookie semantics.
        cookie = f"{name}-{suffix}=; Domain={domain}; Path=/; Max-Age=0; Secure; HttpOnly"
        rules.append({"matches": cookie_match(name), "filters": [
            {"type": "ResponseHeaderModifier", "responseHeaderModifier": {"set": [
                {"name": "Set-Cookie", "value": cookie}, {"name": "Cache-Control", "value": "no-store"}]}},
            {"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 302}}]})
    return rules + [backend]


def resources(namespace, gateway_class, domain, tls_secret, issuer, client_id, client_secret,
              exposure_mode="public", oidc_ca_bundle=None, envoy_proxy_image=None):
    """Build a Gateway, three UI routes, and one shared OIDC policy."""
    namespace = validate_kubernetes_name(namespace, "namespace")
    gateway_class = validate_kubernetes_name(gateway_class, "GatewayClass")
    domain = validate_monitoring_domain(domain)
    tls_secret = validate_kubernetes_name(tls_secret, "TLS Secret name")
    issuer = validate_oidc_issuer(issuer)
    client_id = validate_oidc_client_id(client_id)
    client_secret = validate_kubernetes_name(client_secret, "OIDC Secret name")
    if exposure_mode not in ("public", "internal"):
        raise ValueError("exposure mode must be public or internal")
    if envoy_proxy_image:
        envoy_proxy_image = validate_envoy_proxy_image(envoy_proxy_image)
    labels = {OWNER: "monitoring", PUBLIC_ACCESS: "enabled"}
    hosts = {name: f"{name}.{domain}" for name in COMPONENTS}
    doc = [
        {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway",
         "metadata": {"name": RESOURCE_NAMES["gateway"], "namespace": namespace,
                      "labels": labels.copy()},
         "spec": {"gatewayClassName": gateway_class,
                  "listeners": [{"name": "https", "protocol": "HTTPS", "port": 443,
                                 "hostname": f"*.{domain}",
                                 "tls": {"mode": "Terminate", "certificateRefs": [
                                     {"group": "", "kind": "Secret", "name": tls_secret}]},
                                 "allowedRoutes": {"namespaces": {"from": "Same"}}}]}},
    ]
    if exposure_mode == "internal":
        labels = {OWNER: "monitoring", PUBLIC_ACCESS: "enabled"}
        doc[0]["spec"]["infrastructure"] = {"parametersRef": {
            "group": "gateway.envoyproxy.io", "kind": "EnvoyProxy",
            "name": "atlas-monitoring-nodeport"}}
        doc.insert(0, {"apiVersion": "gateway.envoyproxy.io/v1alpha1", "kind": "EnvoyProxy",
                       "metadata": {"name": "atlas-monitoring-nodeport", "namespace": namespace,
                                    "labels": labels},
                       "spec": {"provider": {"type": "Kubernetes", "kubernetes": {
                           **({"envoyDeployment": {"container": {"image": envoy_proxy_image}}}
                              if envoy_proxy_image else {}),
                           "envoyService": {"type": "NodePort",
                                            "externalTrafficPolicy": "Cluster"}}}}})
    for component, (service, port) in COMPONENTS.items():
        doc.append({"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
                    "metadata": {"name": RESOURCE_NAMES[f"route_{component}"],
                                 "namespace": namespace, "labels": labels.copy()},
                    "spec": {"parentRefs": [{"name": RESOURCE_NAMES["gateway"],
                                              "sectionName": "https"}],
                             "hostnames": [hosts[component]],
                             "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                                        "backendRefs": [{"name": service, "port": port}]}]}})
        if component != "grafana":
            doc[-1]["spec"]["rules"][0]["filters"] = [{
                "type": "RequestHeaderModifier",
                "requestHeaderModifier": {"remove": ["X-Atlas-ID-Token"]}}]
    # Scope the cookie family to this deployment so a partial Grafana session
    # can be renewed without clearing the user's other cookies or the PKCE flow.
    suffix = "atlas-" + hashlib.sha256((namespace + "/" + domain).encode()).hexdigest()[:12]
    grafana = next(obj for obj in doc if obj["metadata"]["name"] == RESOURCE_NAMES["route_grafana"])
    grafana["spec"]["rules"] = grafana_session_rules(grafana["spec"]["rules"][0], domain, suffix)
    provider = {"issuer": issuer}
    if oidc_ca_bundle:
        provider["backendRefs"] = [{"group": "gateway.envoyproxy.io", "kind": "Backend",
                                    "name": "atlas-monitoring-oidc-provider", "port": 443}]
        doc.extend(oidc_trust_resources(issuer, oidc_ca_bundle, namespace))
    doc.append({"apiVersion": "gateway.envoyproxy.io/v1alpha1", "kind": "SecurityPolicy",
                "metadata": {"name": RESOURCE_NAMES["policy"], "namespace": namespace,
                             "labels": labels.copy()},
                "spec": {"targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "Gateway",
                                          "name": RESOURCE_NAMES["gateway"]}],
                         "oidc": {"provider": provider, "clientID": client_id,
                                  "clientSecret": {"name": client_secret},
                                  "redirectURL": f"https://{hosts['grafana']}/oauth2/callback",
                                  "logoutPath": "/logout", "cookieDomain": domain,
                                  "forwardIDToken": {"header": "X-Atlas-ID-Token"},
                                  "cookieNames": {"suffix": suffix},
                                  # The AS currently exposes username via profile; it has no
                                  # user email field or email scope, so don't request email.
                                  "scopes": ["openid", "profile"]}}})
    return doc


def render_yaml(documents):
    yaml = yaml_module()
    return "---\n".join(yaml.safe_dump(item, sort_keys=False, allow_unicode=True)
                         for item in documents)


def _kubectl_json(namespace, kind, name):
    result = subprocess.run(["kubectl", "-n", namespace, "get", kind, name, "-o", "json"],
                            text=True, capture_output=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError("Kubernetes resource status could not be read")
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("Kubernetes returned invalid resource status") from None


def _condition(document, condition_type):
    status = document.get("status", {})
    conditions = list(status.get("conditions", []))
    for collection in ("parents", "ancestors"):
        for parent in status.get(collection, []):
            conditions.extend(parent.get("conditions", []))
    return any(item.get("type") == condition_type and item.get("status") == "True"
               for item in conditions)


def _route_accepted(document):
    parents = document.get("status", {}).get("parents", [])
    return any(_condition({"status": parent}, "Accepted") and
               _condition({"status": parent}, "ResolvedRefs") for parent in parents)


def verify_resources(namespace, timeout=300, expected_address=None, require_oidc_backend=False):
    """Wait for Gateway, its OIDC policy, and all routes to be accepted."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gateway = _kubectl_json(namespace, "gateway", RESOURCE_NAMES["gateway"])
        policy = _kubectl_json(namespace, "securitypolicy", RESOURCE_NAMES["policy"])
        routes = [_kubectl_json(namespace, "httproute", RESOURCE_NAMES[f"route_{name}"])
                  for name in COMPONENTS]
        oidc_backend_ready = True
        if require_oidc_backend:
            _kubectl_json(namespace, "backend", "atlas-monitoring-oidc-provider")
            oidc_tls = _kubectl_json(namespace, "backendtlspolicy",
                                     "atlas-monitoring-oidc-backend-tls")
            oidc_backend_ready = (_condition(oidc_tls, "Accepted")
                                  and _condition(oidc_tls, "ResolvedRefs"))
        addresses = gateway.get("status", {}).get("addresses", [])
        if ((addresses or expected_address) and _condition(gateway, "Programmed") and _condition(policy, "Accepted")
                and oidc_backend_ready and all(_route_accepted(route) for route in routes)):
            reported = [item.get("value") for item in addresses if item.get("value")]
            return reported or [expected_address]
        time.sleep(5)
    raise RuntimeError("Envoy Gateway routes or OIDC policy are not programmed")


def _resolve(host):
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except OSError:
        return []


def _is_private_host(host):
    """Recognize private OIDC names and bypass desktop proxy settings for them."""
    normalized = host.rstrip(".").lower()
    if normalized.endswith((".home.arpa", ".internal", ".local")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None:
        return not address.is_global
    addresses = _resolve(normalized)
    return bool(addresses) and all(not ipaddress.ip_address(item).is_global for item in addresses)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def _gateway_addresses(addresses):
    resolved = set()
    for value in addresses:
        try:
            resolved.add(str(ipaddress.ip_address(value)))
        except ValueError:
            resolved.update(_resolve(value))
    return resolved


def probe_access(domain, issuer, timeout=8, exposure_mode="public", expected_addresses=(),
                 ca_bundle=None):
    """Require DNS to reach Gateway and verify HTTPS/OIDC redirects for every UI hostname."""
    domain, issuer = validate_monitoring_domain(domain), validate_oidc_issuer(issuer)
    if exposure_mode not in ("public", "internal"):
        raise ValueError("exposure mode must be public or internal")
    issuer_host = urlsplit(issuer).hostname
    gateway_ips = _gateway_addresses(expected_addresses)
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    proxy_handler = ProxyHandler({}) if exposure_mode == "internal" else ProxyHandler()
    opener = build_opener(proxy_handler, HTTPSHandler(context=context), _NoRedirect())
    evidence = []
    for component in UI_HOSTS:
        host = f"{component}.{domain}"
        addresses = _resolve(host)
        ips = {str(ipaddress.ip_address(address)) for address in addresses}
        if not addresses:
            evidence.append({"host": host, "state": "failed", "reason": "dns-not-resolved"})
            continue
        if exposure_mode == "public" and not all(ipaddress.ip_address(address).is_global for address in addresses):
            evidence.append({"host": host, "state": "failed", "reason": "dns-not-public"})
            continue
        if exposure_mode == "internal" and not all(
                ipaddress.ip_address(address).is_private
                and not ipaddress.ip_address(address).is_loopback
                and not ipaddress.ip_address(address).is_link_local for address in addresses):
            evidence.append({"host": host, "state": "failed", "reason": "dns-not-private"})
            continue
        if not gateway_ips:
            evidence.append({"host": host, "state": "failed", "reason": "gateway-address-unresolved"})
            continue
        if not (ips & gateway_ips):
            evidence.append({"host": host, "state": "failed", "reason": "dns-does-not-match-gateway"})
            continue
        try:
            opener.open(Request("https://" + host + "/", method="GET"), timeout=timeout)
            evidence.append({"host": host, "state": "failed", "reason": "oidc-redirect-missing"})
        except HTTPError as response:
            location = response.headers.get("Location", "")
            parsed = urlsplit(location)
            if response.code in (302, 303, 307, 308) and parsed.scheme == "https" and parsed.hostname == issuer_host:
                evidence.append({"host": host, "state": "passed", "reason": "https-and-oidc-redirect"})
            else:
                evidence.append({"host": host, "state": "failed", "reason": "unexpected-http-response"})
        except (URLError, OSError, TimeoutError, ValueError):
            evidence.append({"host": host, "state": "failed", "reason": "https-probe-failed"})
    return evidence


def verify_public_access(namespace, domain, issuer, timeout=300, exposure_mode="public",
                         expected_address=None, ca_bundle=None, require_oidc_backend=False):
    domain, issuer = validate_monitoring_domain(domain), validate_oidc_issuer(issuer)
    if exposure_mode not in ("public", "internal"):
        raise ValueError("exposure mode must be public or internal")
    try:
        addresses = verify_resources(namespace, timeout, expected_address, require_oidc_backend)
        resource_state = "passed"
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        addresses = []
        resource_state = "failed"
        print(f"Gateway resource verification: failed ({error})")
    probe_addresses = list(addresses)
    if exposure_mode == "internal" and expected_address:
        probe_addresses.append(expected_address)
    evidence = probe_access(domain, issuer, exposure_mode=exposure_mode,
                            expected_addresses=probe_addresses, ca_bundle=ca_bundle)
    print("Exposure mode: " + ("public" if exposure_mode == "public" else "internal-only"))
    print("Monitoring pages:")
    for item in evidence:
        print(f"  https://{item['host']}  {item['state']} ({item['reason']})")
    print("Envoy Gateway resource status: " + resource_state)
    if exposure_mode == "internal" and expected_address:
        address_label = "Internal HAProxy ingress VIP"
    else:
        address_label = "Envoy Gateway private address" if exposure_mode == "internal" else "Envoy Gateway address"
    resolved_gateway_ips = sorted(_gateway_addresses(addresses))
    if exposure_mode == "internal" and expected_address:
        rendered_addresses = expected_address
        if resolved_gateway_ips:
            rendered_addresses += "; Envoy Gateway node addresses: " + ", ".join(resolved_gateway_ips)
    else:
        rendered_addresses = ", ".join(addresses) if addresses else "not reported"
        if exposure_mode == "internal" and resolved_gateway_ips:
            rendered_addresses += " (private IPs: " + ", ".join(resolved_gateway_ips) + ")"
    print(address_label + ": " + rendered_addresses)
    print("OIDC callback: https://grafana." + domain + "/oauth2/callback")
    return 0 if resource_state == "passed" and evidence and all(
        item["state"] == "passed" for item in evidence) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--gateway-class", default="eg")
    parser.add_argument("--domain")
    parser.add_argument("--tls-secret")
    parser.add_argument("--oidc-issuer")
    parser.add_argument("--oidc-client-id")
    parser.add_argument("--oidc-secret")
    parser.add_argument("--oidc-ca-bundle", help="Private CA PEM bundle for OIDC provider TLS")
    parser.add_argument("--envoy-proxy-image", help="Pinned Envoy Proxy image from the managed Gateway release")
    parser.add_argument("--exposure-mode", choices=("public", "internal"), default="public")
    parser.add_argument("--expected-address", help="Expected ingress VIP for internal HAProxy mode")
    parser.add_argument("--ca-bundle", help="Optional CA PEM bundle for HTTPS verification on this host")
    parser.add_argument("--require-oidc-backend", action="store_true",
                        help="Require the managed OIDC Backend and accepted BackendTLSPolicy")
    parser.add_argument("--check-issuer", action="store_true",
                        help="Verify OIDC discovery reachability and issuer consistency")
    parser.add_argument("--issuer-timeout", type=int, default=8)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.check_issuer:
            verify_oidc_issuer(args.oidc_issuer, args.issuer_timeout, args.ca_bundle)
            print("OIDC discovery verification: passed")
            return 0
        if args.verify:
            if args.timeout < 1 or args.timeout > 1800:
                parser.error("--timeout must be from 1 to 1800 seconds")
            return verify_public_access(args.namespace, args.domain, args.oidc_issuer,
                                        args.timeout, args.exposure_mode, args.expected_address,
                                        args.ca_bundle, args.require_oidc_backend)
        if not args.render:
            parser.error("--render or --verify is required")
        documents = resources(args.namespace, args.gateway_class, args.domain,
                              args.tls_secret, args.oidc_issuer,
                              args.oidc_client_id, args.oidc_secret, args.exposure_mode,
                              args.oidc_ca_bundle, args.envoy_proxy_image)
        sys.stdout.write(render_yaml(documents))
        return 0
    except (TypeError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1 if args.verify else 2


if __name__ == "__main__":
    raise SystemExit(main())
