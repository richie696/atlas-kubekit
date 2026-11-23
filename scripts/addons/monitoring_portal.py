#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install the navigation site on the existing monitoring HTTPS/OIDC gateway."""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from monitoring_public_access import validate_monitoring_domain
from portal.render import files
from sources import registry_ref, validate

NAME = "atlas-operations-portal"
OWNER = "atlas-kubekit.io/managed-by"
LINKS = "atlas-kubekit.io/portal-links"
GATEWAY = "atlas-monitoring-public"
POLICY = "atlas-monitoring-oidc"
IMAGE = "docker.io/library/busybox:1.37.0"
NS = ["kubectl", "-n", "monitoring"]


def get(kind, name):
    output = subprocess.check_output(NS + ["get", kind, name, "--ignore-not-found", "-o", "json"])
    return json.loads(output) if output.strip() else {}


def accepted(status, kind):
    return any(c.get("type") == kind and c.get("status") == "True" for c in status.get("conditions", []))


def require_gateway(domain):
    route = get("httproute", "atlas-monitoring-grafana")
    policy = get("securitypolicy", POLICY)
    gateway = get("gateway", GATEWAY)
    host = f"grafana.{domain}"
    spec = policy.get("spec", {})
    if not (gateway.get("metadata", {}).get("labels", {}).get(OWNER) == "monitoring"
            and host in route.get("spec", {}).get("hostnames", [])
            and any(p.get("name") == GATEWAY and p.get("sectionName") == "https"
                    for p in route.get("spec", {}).get("parentRefs", []))
            and spec.get("oidc", {}).get("provider", {}).get("issuer")
            and any(t.get("name") == GATEWAY and t.get("kind") == "Gateway"
                    for t in spec.get("targetRefs", []))):
        raise ValueError("当前域名缺少已就绪的 Atlas HTTPS 网关或 OIDC 策略，请先完成 19 号入口配置")

    # The first install can reach this point before the Gateway controller reconciles.
    for _ in range(45):
        if (accepted(gateway.get("status", {}), "Programmed") and
                any(a.get("ancestorRef", {}).get("name") == GATEWAY and accepted(a, "Accepted")
                    for a in policy.get("status", {}).get("ancestors", []))):
            return
        time.sleep(2)
        gateway = get("gateway", GATEWAY)
        policy = get("securitypolicy", POLICY)
    raise RuntimeError("等待 HTTPS 网关及 OIDC 策略就绪超时")


def manifests(domain, assets, extra, image):
    selector = {"app.kubernetes.io/name": NAME}
    labels = {**selector, OWNER: NAME}

    def resource(version, kind, **fields):
        return {"apiVersion": version, "kind": kind,
                "metadata": {"name": NAME, "namespace": "monitoring", "labels": labels}, **fields}

    config = resource("v1", "ConfigMap", data=assets)
    config["metadata"]["annotations"] = {LINKS: json.dumps(extra, ensure_ascii=False)}
    digest = hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest()
    container = {
        "name": "web", "image": image, "command": ["httpd", "-f", "-p", "8080", "-h", "/www"],
        "ports": [{"name": "http", "containerPort": 8080}],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "readinessProbe": {"httpGet": {"path": "/index.html", "port": "http"}},
        "resources": {"requests": {"cpu": "5m", "memory": "4Mi"}, "limits": {"memory": "32Mi"}},
        "volumeMounts": [{"name": "pages", "mountPath": "/www", "readOnly": True}]}
    workload = resource("apps/v1", "Deployment", spec={
        "replicas": 2, "selector": {"matchLabels": selector},
        "template": {"metadata": {"labels": selector, "annotations": {"atlas-kubekit.io/content": digest}},
                     "spec": {"automountServiceAccountToken": False, "securityContext": {
                         "runAsNonRoot": True, "runAsUser": 65534, "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [container], "volumes": [{"name": "pages", "configMap": {"name": NAME}}]}}})
    service = resource("v1", "Service", spec={"selector": selector,
                       "ports": [{"name": "http", "port": 80, "targetPort": "http"}]})
    route = resource("gateway.networking.k8s.io/v1", "HTTPRoute", spec={
        "parentRefs": [{"name": GATEWAY, "sectionName": "https"}], "hostnames": [f"ops.{domain}"],
        "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                   "filters": [{"type": "RequestHeaderModifier", "requestHeaderModifier": {
                       "remove": ["Authorization", "Cookie", "X-Atlas-ID-Token"]}},
                       {"type": "ResponseHeaderModifier", "responseHeaderModifier": {"set": [
                           {"name": "X-Content-Type-Options", "value": "nosniff"},
                           {"name": "Content-Security-Policy", "value": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"},
                           {"name": "Cache-Control", "value": "no-cache"}]}}],
                   "backendRefs": [{"name": NAME, "port": 80}]}]})
    legacy = resource("gateway.networking.k8s.io/v1", "HTTPRoute", spec={
        "parentRefs": [{"name": GATEWAY, "sectionName": "https"}],
        "hostnames": [f"grafana.{domain}"],
        "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/ops"}}],
                   "filters": [{"type": "RequestRedirect", "requestRedirect": {
                       "scheme": "https", "hostname": f"ops.{domain}", "statusCode": 302,
                       "path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}}}]}]})
    legacy["metadata"]["name"] = NAME + "-legacy"
    return [config, workload, service, route, legacy]


def route_ready(route):
    generation = route.get("metadata", {}).get("generation")
    for parent in route.get("status", {}).get("parents", []):
        if parent.get("parentRef", {}).get("name") == GATEWAY:
            ready = {c["type"] for c in parent.get("conditions", [])
                     if c.get("observedGeneration") == generation and c.get("status") == "True"}
            if {"Accepted", "ResolvedRefs"} <= ready:
                return True
    return False


def install(domain, source, links_file):
    domain = validate_monitoring_domain(domain)
    require_gateway(domain)
    previous = {}
    keys = [(kind, NAME) for kind in ("ConfigMap", "Deployment", "Service", "HTTPRoute")]
    for kind, name in [*keys, ("HTTPRoute", NAME + "-legacy")]:
        obj = get(kind, name)
        if obj and obj["metadata"].get("labels", {}).get(OWNER) != NAME:
            raise ValueError(f"{kind}/{name} 已被其他安装占用")
        previous[kind, name] = obj
    if links_file:
        extra = json.loads(links_file.read_text())
    else:
        metadata = previous["ConfigMap", NAME].get("metadata", {})
        extra = json.loads(metadata.get("annotations", {}).get(LINKS, "[]"))
    maps = json.loads(subprocess.check_output(NS + ["get", "configmap", "-l", "grafana_dashboard=1",
                                                    "-o", "json"]))["items"]
    deployment = previous["Deployment", NAME]
    if source is None:
        saved_source = deployment.get("metadata", {}).get("annotations", {}).get(
            "atlas-kubekit.io/image-source", '{"profile":"official"}')
        source = json.loads(saved_source)
    source = validate(source)
    image = registry_ref(IMAGE, source)
    for obj in manifests(domain, files(domain, maps, extra), extra, image):
        if obj["kind"] == "Deployment":
            obj["metadata"]["annotations"] = {"atlas-kubekit.io/image-source": json.dumps(source)}
        # Creation fails on a concurrent owner; apply only to resources checked above.
        action = ["apply"] if previous[obj["kind"], obj["metadata"]["name"]] else ["create", "--save-config"]
        subprocess.run(NS + action + ["-f", "-"], input=json.dumps(obj).encode(), check=True)
    subprocess.run(NS + ["rollout", "status", "deployment/" + NAME, "--timeout=3m"], check=True)
    for _ in range(45):
        if all(route_ready(get("httproute", name)) for name in (NAME, NAME + "-legacy")):
            print(f"Atlas KubeKit 运维门户  https://ops.{domain}/")
            return
        time.sleep(2)
    raise RuntimeError("门户路由未就绪，请检查 HTTPRoute 状态后重试")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--links", type=Path, help="已认证的集群工具 HTTPS 入口 JSON")
    args = parser.parse_args()
    try:
        install(args.domain, json.loads(args.source_config.read_text()) if args.source_config else
                None, args.links)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"门户安装失败：{error}\n")
