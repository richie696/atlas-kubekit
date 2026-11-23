# SPDX-License-Identifier: MIT
"""Build the static directory from published hosts and provisioned dashboards."""
import json
import re
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

ASSETS = Path(__file__).parent
UID = re.compile(r"[A-Za-z0-9_-]{1,40}")
PAGES = {
    "Grafana · 看板与探索": ("grafana", (
        ("看板目录", "/dashboards"), ("指标与日志探索", "/explore"),
        ("数据源管理", "/connections/datasources"))),
    "Prometheus · 指标、规则与采集": ("prometheus", (
        ("指标查询", "/query"), ("告警状态", "/alerts"), ("规则健康", "/rules"),
        ("采集目标", "/targets"), ("服务发现", "/service-discovery"),
        ("告警接收端发现", "/alertmanager-discovery"), ("运行信息", "/status"),
        ("时间序列库状态", "/tsdb-status"), ("生效配置", "/config"), ("启动参数", "/flags"))),
    "Alertmanager · 告警与静默": ("alertmanager", (
        ("当前告警", "/#/alerts"), ("静默维护", "/#/silences"), ("创建静默", "/#/silences/new"),
        ("实例状态", "/#/status"), ("界面偏好", "/#/settings"))),
}


def registered_links(entries):
    if not isinstance(entries, list):
        raise ValueError("门户附加入口须为 JSON 数组")
    grouped = {}
    for row in entries:
        if not isinstance(row, dict) or set(row) != {"group", "name", "url"}:
            raise ValueError("每个入口需要 group、name 和 url")
        group, name, url = (row[k] for k in ("group", "name", "url"))
        if not all(isinstance(s, str) and s.strip() for s in (group, name, url)):
            raise ValueError("入口字段不能为空")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or "\\" in url
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError("入口必须是无凭据、无查询参数的 HTTPS 地址")
        grouped.setdefault(group, []).append((name, url))
    return grouped


def dashboard_links(configmaps, base):
    links = set()
    for item in configmaps:
        for name, raw in item.get("data", {}).items():
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            uid, title = data.get("uid"), data.get("title")
            if isinstance(uid, str) and UID.fullmatch(uid) and isinstance(title, str) and title:
                links.add((title, f"{base}/d/{uid}"))
    return sorted(links)


def files(domain, configmaps, extra):
    def url(host, path):
        return f"https://{host}.{domain}{path}"

    groups = [(title, [(name, url(host, path)) for name, path in links])
              for title, (host, links) in PAGES.items()]
    dashboards = dashboard_links(configmaps, url("grafana", ""))
    if dashboards:
        groups.append(("Grafana · 集群与组件看板", dashboards))
    groups.extend(registered_links(extra).items())
    sections = []
    for title, entries in groups:
        links = "".join(f'<a target="_blank" rel="noopener noreferrer" href="{escape(href)}">{escape(name)}</a>' for name, href in entries)
        sections.append(f'<details open><summary>{escape(title)}</summary><div class="links">{links}</div></details>')
    tasks = (("集群看板", "资源与工作负载", "grafana", "/dashboards"),
             ("查询日志", "Grafana Explore", "grafana", "/explore"),
             ("采集目标", "Prometheus Targets", "prometheus", "/targets"),
             ("当前告警", "Alertmanager", "alertmanager", "/#/alerts"))
    shortcuts = "".join(f'<a target="_blank" rel="noopener noreferrer" href="{escape(url(host, path))}"><strong>{label}</strong><small>{hint}</small></a>'
                        for label, hint, host, path in tasks)
    page = Template((ASSETS / "index.html").read_text()).substitute(shortcuts=shortcuts, groups="\n".join(sections))
    return {"index.html": page, **{name: (ASSETS / name).read_text() for name in ("portal.css", "portal.js")}}
