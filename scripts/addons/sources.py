#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Phase 3 download policy; also embedded in the standalone entrypoint."""
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


PROFILES = {"official", "daocloud", "1panel", "google", "custom"}
PUBLIC_REGISTRIES = {
    "docker.io", "quay.io", "ghcr.io", "gcr.io", "k8s.gcr.io",
    "registry.k8s.io", "mcr.microsoft.com", "nvcr.io", "docker.elastic.co",
}
# Exact HTTPS download authorities observed from DaoCloud's file service.
# Do not trust arbitrary subdomains, object stores, or redirects to upstream.
DAOCLOUD_FILE_HOSTS = {"files.m.daocloud.io", "files-mirror.r2.daocloud.vip"}
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\+[a-zA-Z0-9.-]+)?$")
RELEASE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$")


def tr(key, *values):
    locale = os.environ.get("ATLAS_LANG", "en")
    if locale.casefold() == "zh":
        locale = "zh_CN"
    roots = []
    if os.environ.get("ATLAS_LOCALES_DIR"):
        roots.append(Path(os.environ["ATLAS_LOCALES_DIR"]))
    roots.append(Path(__file__).resolve().parents[2] / "locales")
    message = None
    for root in roots:
        for code in (locale, "en"):
            normalized = code.replace("-", "_").casefold()
            path = next((candidate for candidate in root.glob("*.json")
                         if candidate.stem.replace("-", "_").casefold() == normalized), root / f"{code}.json")
            try:
                message = json.loads(path.read_text(encoding="utf-8"))["messages"].get(key)
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if message is not None:
                break
        if message is not None:
            break
    if message is None:
        message = key
    return message % values if values else message


def yaml_module():
    try:
        import yaml
    except ImportError:
        raise ValueError("python3-yaml is required for add-on metadata: sudo apt-get install python3-yaml") from None
    return yaml


def https_url(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c.isspace() for c in value)):
        raise ValueError("Use an HTTPS URL without credentials, query parameters or fragments")
    if any(part in {".", ".."} for part in url.path.split("/")):
        raise ValueError("Relative path segments are not supported")
    return value.rstrip("/")


def validate(config):
    if not isinstance(config, dict) or config.get("profile") not in PROFILES:
        raise ValueError("Invalid download profile")
    result = {"profile": config["profile"]}
    previous = config.get("previous_registry_prefixes", [])
    if not isinstance(previous, list) or len(previous) > 20:
        raise ValueError("Invalid previous registry prefixes")
    result["previous_registry_prefixes"] = [https_url("https://" + value).removeprefix("https://") for value in previous]
    if result["profile"] == "custom":
        result["files"] = https_url(config.get("files", ""))
        registry = https_url("https://" + config.get("registry", ""))
        result["registry"] = registry.removeprefix("https://")
    return result


def file_url(url, config):
    https_url(url)
    profile = config["profile"]
    if profile in {"official", "google"}:
        return url
    prefix = config["files"] if profile == "custom" else "https://files.m.daocloud.io"
    if url.startswith(prefix + "/"):
        return url
    return prefix + "/" + url.removeprefix("https://")


def registry_ref(ref, config):
    """Preserve the repository, tag and digest. Never redirect private registries."""
    if not ref:
        return ref
    prefixes = config.get("previous_registry_prefixes", [])
    if config["profile"] == "custom":
        prefixes = prefixes + [config["registry"]]
    for prefix in sorted(prefixes, key=len, reverse=True):
        if ref.startswith(prefix + "/"):
            suffix = ref[len(prefix) + 1:]
            if suffix.partition("/")[0] in PUBLIC_REGISTRIES:
                ref = suffix
                break
    # A previously mirrored Helm release may be explicitly upgraded with a new
    # selection. Known public mirror names have an unambiguous upstream mapping.
    if ref.startswith("m.daocloud.io/"):
        ref = ref.removeprefix("m.daocloud.io/")
    elif ref.startswith(("docker.1panel.live/", "mirror.gcr.io/")):
        ref = "docker.io/" + ref.partition("/")[2]
    if config["profile"] == "official":
        return ref
    first, slash, rest = ref.partition("/")
    explicit = "." in first or ":" in first or first == "localhost"
    if not slash:
        registry, path = "docker.io", "library/" + ref
    elif not explicit:
        registry, path = "docker.io", ref
    else:
        registry, path = first, rest
    if registry in {"registry-1.docker.io", "index.docker.io"}:
        registry = "docker.io"
    if registry == "docker.io" and "/" not in path:
        path = "library/" + path
    if registry not in PUBLIC_REGISTRIES:
        return ref
    if config["profile"] == "google":
        return "mirror.gcr.io/" + path if registry == "docker.io" else ref
    if config["profile"] == "1panel" and registry == "docker.io":
        return "docker.1panel.live/" + path
    prefix = config["registry"] if config["profile"] == "custom" else "m.daocloud.io"
    return prefix + "/" + registry + "/" + path


def validate_download_redirect(target, newurl, config):
    destination = urllib.parse.urlsplit(newurl)
    if (destination.scheme != "https" or not destination.hostname or destination.username
            or destination.password or any(char.isspace() for char in newurl)):
        raise ValueError(tr("phase03.source.error.unsafe_redirect"))
    if config["profile"] in {"daocloud", "1panel", "custom"}:
        initial = urllib.parse.urlsplit(target)
        allowed = {(initial.hostname, initial.port or 443)}
        if config["profile"] in {"daocloud", "1panel"} and initial.hostname in DAOCLOUD_FILE_HOSTS and initial.port in {None, 443}:
            allowed.update((host, 443) for host in DAOCLOUD_FILE_HOSTS)
        if (destination.hostname, destination.port or 443) not in allowed:
            # A host-only error avoids leaking signed object-storage query strings.
            raise ValueError(tr("phase03.source.error.unapproved_redirect", destination.hostname))


def fetch(url, destination, config, limit):
    target = file_url(url, config)
    label = tr("phase03.source.label.download")
    print(label + ": " + target, file=sys.stderr)
    request = urllib.request.Request(target, headers={"User-Agent": "Atlas-KubeKit/03"})
    # Signed HTTPS CDN URLs may contain query parameters; do not log their tokens.
    # A mirror redirect to upstream would silently bypass the user's selection.
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            validate_download_redirect(target, newurl, config)
            before = urllib.parse.urlsplit(request.full_url).hostname
            after = urllib.parse.urlsplit(newurl).hostname
            if before != after:
                print(tr("phase03.source.message.redirect", before, after), file=sys.stderr)
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(Redirect())
    partial = Path(str(destination) + ".part")
    try:
        with opener.open(request, timeout=30) as response, partial.open("wb") as out:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise ValueError("Download exceeds size limit")
                out.write(chunk)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def index(repo, name, destination, config):
    raw = Path(str(destination) + ".yaml")
    fetch(repo.rstrip("/") + "/index.yaml", raw, config, 64 * 1024 * 1024)
    data = yaml_module().safe_load(raw.read_text())
    rows = data.get("entries", {}).get(name, []) if isinstance(data, dict) else []
    rows = [row for row in rows if isinstance(row, dict) and RELEASE.fullmatch(str(row.get("version", "")))]
    stable = [row for row in rows if VERSION.fullmatch(str(row["version"]))]
    if not stable:
        raise ValueError("No stable chart version found in the selected source for " + name)
    latest = max(stable, key=lambda row: tuple(map(int, VERSION.fullmatch(str(row["version"])).groups())))
    Path(destination).write_text(json.dumps({"repo": repo, "name": name, "entries": rows}, default=str))
    print(latest["version"])


class CompatibilityTables(HTMLParser):
    """Read cells, not unrelated version numbers in navigation or prose."""
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def minor_series(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(?:\d+|x))?", str(value).strip())
    return ".".join(match.groups()) if match else None


def documented_range(value, listed=False):
    """Only accept the explicit minor lists/ranges used by these upstream tables."""
    value = value.strip().strip("*` ")
    if re.fullmatch(r"v?\d+\.\d+\+", value):
        return ">=" + value.lstrip("v").rstrip("+") + ".0-0"
    if listed and re.fullmatch(r"v?\d+\.\d+(?:\s*,\s*v?\d+\.\d+)*", value):
        return " || ".join("~" + part.strip().lstrip("v") + ".0-0" for part in value.split(","))
    match = re.fullmatch(r"v?(\d+)\.(\d+)\s*(?:→|–|—|-)\s*v?(\d+)\.(\d+)", value)
    if not match:
        return None
    first_major, first_minor, last_major, last_minor = map(int, match.groups())
    return f">={first_major}.{first_minor}.0-0 <{last_major}.{last_minor + 1}.0-0"


MATRICES = {
    "metrics-server": "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/master/README.md",
    "cert-manager": "https://cert-manager.io/docs/releases/",
    "gateway-helm": "https://gateway.envoyproxy.io/news/releases/matrix/",
    "keda": "https://keda.sh/docs/latest/operate/cluster/",
    "kyverno": "https://kyverno.io/docs/installation/releases/",
}


def matrix_ranges(name, text):
    ranges = {}
    if name == "metrics-server":
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and minor_series(cells[0]) and (constraint := documented_range(cells[2])):
                ranges[minor_series(cells[0])] = constraint
        return ranges
    parser = CompatibilityTables()
    parser.feed(text)
    for table in parser.tables:
        if not table:
            continue
        header = [cell.lower() for cell in table[0]]
        if name == "kyverno":
            # Current support table is transposed: label/value, not one release per row.
            fields = {row[0].lower().rstrip(":"): row[1] for row in table if len(row) == 2}
            series = re.match(r"v?\d+\.\d+", fields.get("supported release", ""))
            constraint = documented_range(fields.get("kubernetes versions supported", ""))
            if series and constraint:
                ranges[minor_series(series[0])] = constraint
            if header == ["kyverno version", "kubernetes min", "kubernetes max"]:
                for row in table[1:]:
                    if len(row) == 3 and minor_series(row[0]) and (constraint := documented_range(row[1] + " - " + row[2])):
                        ranges[minor_series(row[0])] = constraint
        column = next((i for i, title in enumerate(header)
                       if title in {"kubernetes", "kubernetes version"}
                       or title.startswith("supported kubernetes /")), None)
        if column is None:
            continue
        for row in table[1:]:
            if len(row) <= column or not (series := minor_series(row[0])):
                continue
            cell = row[column].split("/")[0].strip()
            constraint = documented_range(cell, listed=name == "gateway-helm")
            if constraint:
                ranges[series] = constraint
    return ranges


def compatibility_policy(name, destination, config):
    policy = {"source": MATRICES.get(name), "ranges": {}}
    if policy["source"]:
        raw = Path(str(destination) + ".upstream")
        try:
            fetch(policy["source"], raw, config, 4 * 1024 * 1024)
            policy["ranges"] = matrix_ranges(name, raw.read_text())
            if not policy["ranges"]:
                raise ValueError("No recognizable Kubernetes compatibility rows")
        except Exception as exc:
            policy["unavailable"] = str(exc)
            print("[WARN] " + str(exc), file=sys.stderr)
    Path(destination).write_text(json.dumps(policy))


_constraint_cache = {}


def helm_matches(constraint, kubernetes):
    """Use Helm's own SemVer evaluator, including OR, wildcards and prereleases."""
    key = (constraint, kubernetes)
    if key not in _constraint_cache:
        with tempfile.TemporaryDirectory(prefix="atlas-compat-") as directory:
            chart = {"apiVersion": "v2", "name": "atlas-compatibility", "version": "0.1.0", "kubeVersion": constraint}
            Path(directory, "Chart.yaml").write_text(yaml_module().safe_dump(chart))
            result = subprocess.run(["helm", "template", "atlas-compatibility", directory,
                                     "--kube-version", kubernetes], capture_output=True, text=True, timeout=30)
        if result.returncode and "is incompatible with Kubernetes" not in result.stderr:
            raise ValueError("Helm cannot evaluate the Kubernetes constraint: " + result.stderr.strip())
        _constraint_cache[key] = result.returncode == 0
    return _constraint_cache[key]


def compatibility(chart, kubernetes, policy):
    if chart.get("deprecated"):
        return "blocked", tr("phase03.source.compat.deprecated")
    constraint = str(chart.get("kubeVersion") or "").strip()
    if constraint and not helm_matches(constraint, kubernetes):
        return "blocked", tr("phase03.source.compat.chart_kube_version", constraint)
    if policy.get("source"):
        upstream = policy["ranges"].get(minor_series(chart.get("appVersion", "")))
        if not upstream:
            return "unknown", tr("phase03.source.compat.upstream_matrix_missing", policy["source"])
        if not helm_matches(upstream, kubernetes):
            return "blocked", tr("phase03.source.compat.outside_upstream", upstream, policy["source"])
        return "upstream", tr("phase03.source.compat.upstream_range", upstream, constraint or tr("phase03.source.compat.undeclared"), policy["source"])
    if constraint:
        return "chart", tr("phase03.source.compat.chart_only", constraint)
    return "unknown", tr("phase03.source.compat.no_rule")


def recommend_version(metadata, kubernetes, policy_path):
    data = json.loads(Path(metadata).read_text())
    policy = json.loads(Path(policy_path).read_text())
    rows = sorted((row for row in data["entries"] if VERSION.fullmatch(str(row.get("version", "")))),
                  key=lambda row: tuple(map(int, VERSION.fullmatch(str(row["version"])).groups())), reverse=True)
    unknown, skipped = None, []
    for row in rows:
        state, reason = compatibility(row, kubernetes, policy)
        if state in {"chart", "upstream"}:
            print(f"[INFO] Kubernetes {kubernetes}: {data['name']} {row['version']} ({state}); {reason}", file=sys.stderr)
            print(row["version"])
            return
        if state == "unknown" and unknown is None and not policy["ranges"]:
            unknown = row
        if state == "blocked" and len(skipped) < 3:
            skipped.append(str(row["version"]) + ": " + reason)
    if unknown:
        print(tr("phase03.source.message.compat_unconfirmed", unknown["version"]), file=sys.stderr)
        print(unknown["version"])
        return
    raise ValueError(tr("phase03.source.error.no_compatible_release", kubernetes, "; ".join(skipped)))


def envoy_index(destination, config):
    # Official release-note links discover versions; no Gateway version is pinned here.
    raw = Path(str(destination) + ".html")
    url = "https://gateway.envoyproxy.io/news/releases/notes/"
    fetch(url, raw, config, 4 * 1024 * 1024)
    versions = set(re.findall(r'/news/releases/notes/(v\d+\.\d+\.\d+)/', raw.read_text()))
    if not versions:
        raise ValueError("No stable Envoy Gateway releases in official release metadata")
    Path(destination).write_text(json.dumps({"name": "gateway-helm", "entries": [
        {"version": version, "appVersion": version} for version in versions]}))


def check_chart_compatibility(metadata, kubernetes, policy_path):
    chart = yaml_module().safe_load(Path(metadata).read_text())
    if not isinstance(chart, dict):
        raise ValueError("Invalid Chart metadata")
    state, reason = compatibility(chart, kubernetes, json.loads(Path(policy_path).read_text()))
    print(f"[{state.upper()}] {chart.get('name')} Chart={chart.get('version')} app={chart.get('appVersion')} Kubernetes={kubernetes}: {reason}", file=sys.stderr)
    if state == "blocked":
        raise ValueError("Selected add-on version is incompatible; installation stopped")
    print(state)


def upgrade_direction(release_path, chart_name, version):
    releases = json.loads(Path(release_path).read_text())
    if not isinstance(releases, list) or len(releases) != 1:
        raise ValueError("Cannot identify the existing Helm release version")
    label = str(releases[0].get("chart", ""))
    prefix = chart_name + "-"
    current = label.removeprefix(prefix)
    if not label.startswith(prefix) or not RELEASE.fullmatch(current) or not RELEASE.fullmatch(version):
        raise ValueError("Cannot compare the existing Helm chart version: " + label)
    def order(value):
        core, _, prerelease = value.removeprefix("v").split("+", 1)[0].partition("-")
        identifiers = tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".")) if prerelease else ()
        return (*map(int, core.split(".")), not bool(prerelease), identifiers)
    print("downgrade" if order(version) < order(current) else "same-or-upgrade")


def check_apis(manifest, versions):
    yaml = yaml_module()
    def expand(doc):
        if not isinstance(doc, dict):
            return []
        if doc.get("kind") == "List":
            return [item for child in doc.get("items", []) for item in expand(child)]
        return [doc]
    resources = [item for doc in yaml.safe_load_all(Path(manifest).read_text()) for item in expand(doc)]
    available = set(Path(versions).read_text().splitlines())
    # Fresh-install CRDs can provide APIs used by the same Chart's resources.
    for doc in resources:
        if doc.get("kind") == "CustomResourceDefinition":
            spec = doc.get("spec", {})
            available.update(spec.get("group", "") + "/" + version["name"] for version in spec.get("versions", [])
                             if version.get("served") and version.get("name"))
    missing = sorted({str(doc.get("apiVersion")) + " " + str(doc.get("kind")) for doc in resources
                      if doc.get("apiVersion") not in available})
    if missing:
        raise ValueError(tr("phase03.source.error.unavailable_apis", ", ".join(missing)))


def archive(metadata, version, destination, config):
    data = json.loads(Path(metadata).read_text())
    row = next((r for r in data["entries"] if str(r["version"]).removeprefix("v") == version.removeprefix("v")), None)
    if not row or not isinstance(row.get("urls"), list) or not row["urls"]:
        raise ValueError("The selected chart version is absent from this source")
    url = urllib.parse.urljoin(data["repo"].rstrip("/") + "/", row["urls"][0])
    fetch(url, destination, config, 256 * 1024 * 1024)
    digest = str(row.get("digest", "")).removeprefix("sha256:")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("The chart index does not provide a valid SHA-256 digest")
    if hashlib.sha256(Path(destination).read_bytes()).hexdigest() != digest.lower():
        Path(destination).unlink()
        raise ValueError("Chart SHA-256 mismatch; installation stopped")


def rewrite_spec(value, config, changes):
    """Rewrite image fields and operator image flags, never arbitrary text."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"image", "imageRepository"} and isinstance(child, str) and child:
                mapped = registry_ref(child, config)
                if mapped != child:
                    changes.add((child, mapped))
                    value[key] = mapped
            elif key in {"args", "command"} and isinstance(child, list):
                image_flags = {"--prometheus-config-reloader", "--config-reloader-image",
                               "--prometheus-default-base-image", "--alertmanager-default-base-image",
                               "--thanos-default-base-image", "--acme-http01-solver-image"}
                for i, argument in enumerate(child):
                    if not isinstance(argument, str):
                        continue
                    flag, equal, image = argument.partition("=")
                    if flag in image_flags and equal:
                        mapped = registry_ref(image, config)
                        if mapped != image:
                            changes.add((image, mapped))
                            child[i] = flag + "=" + mapped
                    elif argument in image_flags and i + 1 < len(child):
                        image = child[i + 1]
                        if isinstance(image, str):
                            mapped = registry_ref(image, config)
                            if mapped != image:
                                changes.add((image, mapped))
                                child[i + 1] = mapped
            else:
                rewrite_spec(child, config, changes)
    elif isinstance(value, list):
        for child in value:
            rewrite_spec(child, config, changes)


def merge_values(left, right):
    result = dict(left)
    for key, value in right.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = value
    return result


def image_values(value, config):
    """Return only changed image values, including those used by Helm hooks.

    Helm excludes hooks from its post-renderer. Repository/registry pairs and
    full image keys in values cover those templates before Helm renders them.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, child in value.items():
        if isinstance(child, dict):
            changed = image_values(child, config)
            if changed:
                result[key] = changed
        elif key in {"image", "imageRepository"} and isinstance(child, str) and child:
            mapped = registry_ref(child, config)
            if mapped != child:
                result[key] = mapped
    repository = value.get("repository")
    # Only image objects, not Git/Helm repository configuration.
    is_image = any(key in value for key in ("tag", "pullPolicy", "digest", "sha"))
    if isinstance(repository, str) and repository and is_image:
        registry = value.get("registry")
        full = (registry.rstrip("/") + "/" if isinstance(registry, str) and registry else "") + repository
        mapped = registry_ref(full, config)
        if mapped != full:
            if "registry" in value:
                new_registry, _, new_repo = mapped.partition("/")
                result["registry"], result["repository"] = new_registry, new_repo
            else:
                result["repository"] = mapped
    return result


def values(paths, config):
    yaml = yaml_module()
    merged = load_values(paths)
    # A global private registry may override every per-image registry in Helm.
    # Do not inject a public mirror into repositories under that private prefix.
    global_values = merged.get("global") or {}
    global_registry = global_values.get("imageRegistry", "") if isinstance(global_values, dict) else ""
    if global_registry and global_registry not in PUBLIC_REGISTRIES:
        overrides = {}
    else:
        overrides = image_values(merged, config)
    yaml.safe_dump(overrides, sys.stdout, sort_keys=False)


def envoy_controller(metadata, paths, config):
    yaml = yaml_module()
    merged = load_values(paths)
    global_values = merged.get("global") or {}
    legacy = merged.get("deployment", {}).get("envoyGateway", {}).get("image") or {}
    chart = yaml.safe_load(Path(metadata).read_text()) or {}
    image = global_values.get("images", {}).get("envoyGateway", {}).get("image")
    if legacy.get("repository") or not image:
        # Match eg.image: legacy repository uses appVersion as default tag;
        # the chart's built-in controller image uses Chart.Version.
        tag = str(legacy.get("tag") or (chart.get("appVersion") if legacy.get("repository") else chart.get("version")) or "")
        if not tag:
            raise ValueError("Envoy chart has no controller image/version; provide global.images.envoyGateway.image")
        repository = legacy.get("repository") or "docker.io/envoyproxy/gateway"
        image = repository + ":" + tag
        registry = global_values.get("imageRegistry")
        if registry:
            path = image.partition("/")[2] if "." in image.partition("/")[0] else image
            image = registry.rstrip("/") + "/" + path
    # Certgen's pre-install Job is a hook and bypasses the post-renderer.
    return registry_ref(image, config)


def load_values(paths):
    merged = {}
    for path in paths:
        layer = yaml_module().safe_load(Path(path).read_text()) or {}
        if not isinstance(layer, dict):
            raise ValueError("Helm values must be a YAML mapping")
        merged = merge_values(merged, layer)
    return merged


def nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def proxy_settings(values):
    image = nested(values, "global", "images", "envoyProxy", "image")
    configured = []
    for kind in ("envoyDeployment", "envoyDaemonSet"):
        container = nested(values, "config", "envoyGateway", "envoyProxy", "provider", "kubernetes", kind, "container")
        if isinstance(container, dict):
            if container.get("image") and container.get("imageRepository"):
                raise ValueError("Envoy Proxy image and imageRepository are mutually exclusive")
            configured.extend(str(container[key]) for key in ("image", "imageRepository") if container.get(key))
    if image and configured:
        raise ValueError("Set either global.images.envoyProxy.image or config.envoyGateway.envoyProxy container image settings")
    return image, configured


def has_proxy_override(values):
    proxy = nested(values, "global", "images", "envoyProxy")
    if isinstance(proxy, dict) and "image" in proxy:
        return True
    for kind in ("envoyDeployment", "envoyDaemonSet"):
        container = nested(values, "config", "envoyGateway", "envoyProxy", "provider", "kubernetes", kind, "container")
        if isinstance(container, dict) and {"image", "imageRepository"} & container.keys():
            return True
    return False


def valid_proxy_image(image):
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_@+-]*", image):
        raise ValueError("Invalid Envoy Proxy image reference in upstream metadata or values")
    name, _, digest = image.partition("@sha256:")
    if "@" in image and not re.search(r"@sha256:[a-fA-F0-9]{64}$", image):
        raise ValueError("Invalid Envoy Proxy image digest")
    if digest and not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("Invalid Envoy Proxy image digest")
    tag = name.rpartition("/")[2].partition(":")[2]
    if not tag and not digest:
        raise ValueError("Envoy Proxy image metadata must specify a tag or digest")
    return image


class ReleaseHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.heading = []
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self.in_heading = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_heading = False

    def handle_data(self, data):
        self.text.append(data)
        if self.in_heading:
            self.heading.append(data)


def release_proxy_image(text, version, html=False):
    """Accept only an explicit Helm image assignment on this release's page."""
    if html:
        parser = ReleaseHTML()
        parser.feed(text)
        if "".join(parser.heading).strip() != version:
            raise ValueError("Release page version does not match the selected Envoy Gateway version")
        text = "".join(parser.text)
    else:
        frontmatter = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
        header = yaml_module().safe_load(frontmatter[1]) if frontmatter else {}
        if not isinstance(header, dict) or str(header.get("title", "")) != version:
            raise ValueError("Release document version does not match the selected Envoy Gateway version")
    images = set(re.findall(r"--set(?:-string)?(?:\s+|=)['\"]?global\.images\.envoyProxy\.image=['\"]?(docker\.io/envoyproxy/envoy:[a-zA-Z0-9_.:@+-]+)", text))
    if len(images) > 1:
        raise ValueError("Ambiguous Envoy Proxy image recommendations in the selected release notes")
    return valid_proxy_image(next(iter(images))) if images else None


def version_proxy_image(version, directory, config, chart_default):
    """Network effects stay here; parsing above never guesses a version."""
    warnings = []
    documents = (
        (f"https://gateway.envoyproxy.io/news/releases/notes/{version}/", True),
        (f"https://raw.githubusercontent.com/envoyproxy/gateway/main/site/content/en/news/releases/notes/{version}.md", False),
    )
    for index, (url, html) in enumerate(documents):
        destination = directory / f"release-{index}.txt"
        try:
            fetch(url, destination, config, 2 * 1024 * 1024)
        except (OSError, ValueError) as exc:
            warnings.append(f"{url}: {exc}")
            continue
        # A wrong version or contradictory recommendation is not a missing file.
        image = release_proxy_image(destination.read_text(), version, html)
        if image:
            return image, url, warnings
    if chart_default:
        return valid_proxy_image(chart_default), "selected Helm chart defaults", warnings
    for filename in ("shared_types.go", "envoyproxy_types.go"):
        url = f"https://raw.githubusercontent.com/envoyproxy/gateway/{version}/api/v1alpha1/{filename}"
        destination = directory / filename
        try:
            fetch(url, destination, config, 2 * 1024 * 1024)
        except (OSError, ValueError) as exc:
            warnings.append(f"{url}: {exc}")
            continue
        matches = re.findall(r'(?m)^\s*DefaultEnvoyProxyImage\s*=\s*"([^"\n]+)"', destination.read_text())
        if len(matches) == 1:
            return valid_proxy_image(matches[0]), url, warnings
    raise ValueError("Cannot resolve the Envoy Proxy image for " + version + "; check source connectivity/upstream metadata. " + "; ".join(warnings))


def resolve_envoy_proxy(metadata, output, paths, config):
    if len(paths) < 2:
        raise ValueError("Expected chart defaults, existing release values, then optional user values")
    yaml = yaml_module()
    chart = yaml.safe_load(Path(metadata).read_text()) or {}
    if chart.get("name") != "gateway-helm":
        raise ValueError("Unexpected Envoy Gateway chart identity")
    app = str(chart.get("appVersion") or "")
    version = "v" + app.removeprefix("v")
    if not RELEASE.fullmatch(version):
        raise ValueError("Envoy Gateway chart must declare a release appVersion")
    defaults = load_values(paths[:1])
    previous = load_values(paths[1:2])
    explicit = load_values(paths[2:])
    explicit_image, explicit_config = proxy_settings(explicit)
    previous_image, previous_config = proxy_settings(previous)
    selected_image, selected_config = explicit_image, explicit_config
    origin = "user Helm values"
    previous_prefixes = config.get("previous_registry_prefixes", [])
    if config["profile"] == "custom":
        previous_prefixes = previous_prefixes + [config["registry"]]
    official = {**config, "profile": "official", "previous_registry_prefixes": previous_prefixes}
    # Keep existing custom/private images; normal upstream images follow the new
    # selected release. Re-pin upstream images explicitly in an additional file.
    if not selected_image and not selected_config and not has_proxy_override(explicit):
        if previous_config:
            selected_config, origin = previous_config, "existing Envoy Proxy container configuration"
        elif previous_image:
            canonical = registry_ref(previous_image, official)
            if not re.match(r"^(?:(?:docker\.io|registry-1\.docker\.io|index\.docker\.io)/)?envoyproxy/envoy(?::|@sha256:)", canonical):
                selected_image, origin = previous_image, "existing custom Envoy Proxy image"
    warnings = []
    if selected_config:
        # Let existing/extra config supply image or imageRepository, not a second
        # conflicting global image. The renderer applies the chosen mirror.
        policy = {"global": {"images": {"envoyProxy": {"image": ""}}}}
        display = ", ".join(selected_config)
    else:
        if not selected_image:
            chart_image, chart_config = proxy_settings(defaults)
            if chart_config:
                policy = {"global": {"images": {"envoyProxy": {"image": ""}}}}
                display, origin = ", ".join(chart_config), "selected Helm chart container configuration"
            else:
                selected_image, origin, warnings = version_proxy_image(version, Path(output).parent, config, chart_image)
        if selected_image:
            selected_image = valid_proxy_image(selected_image)
            policy = {"global": {"images": {"envoyProxy": {"image": selected_image}}}}
            display = selected_image
    for warning in warnings:
        print("[WARN] " + warning, file=sys.stderr)
    label = tr("phase03.source.label.envoy_image")
    source_label = tr("phase03.source.label.source")
    display_origin = origin
    display_origin = tr("phase03.source.origin." + origin_key(origin))
    print(tr("phase03.source.message.envoy_image", label, version, display, source_label, display_origin), file=sys.stderr)
    Path(output).write_text(yaml.safe_dump(policy, sort_keys=False))
    Path(output + ".json").write_text(json.dumps({"gatewayVersion": version, "chartVersion": chart.get("version"), "image": display, "source": origin}, indent=2))


def origin_key(origin):
    known = {
        "user Helm values": "user_values",
        "existing Envoy Proxy container configuration": "existing_container_config",
        "existing custom Envoy Proxy image": "existing_custom_image",
        "selected Helm chart defaults": "chart_defaults",
        "selected Helm chart container configuration": "chart_container_config",
    }
    return known.get(origin, "unknown")


def render(config, language):
    yaml = yaml_module()
    # Helm charts may emit empty YAML documents between `---` separators.
    # PyYAML parses those as None; serializing them again writes `null` as a
    # document, which Helm's install/upgrade manifest decoder rejects.
    docs = [doc for doc in yaml.safe_load_all(sys.stdin) if doc is not None]
    changes = set()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        if kind in {"CustomResourceDefinition", "Secret"}:
            continue
        if kind == "List":
            # Helm emits separate documents; fail rather than miss a nested manifest.
            raise ValueError("List manifests need expansion before image-source rewriting")
        rewrite_spec(doc.get("spec", {}), config, changes)
        if kind == "ConfigMap":
            # Envoy's controller creates data-plane Pods later using this config.
            for key, content in doc.get("data", {}).items():
                if key == "envoy-gateway.yaml" and isinstance(content, str):
                    nested = yaml.safe_load(content)
                    rewrite_spec(nested, config, changes)
                    doc["data"][key] = yaml.safe_dump(nested, sort_keys=False)
    label = tr("phase03.source.label.image_source")
    for before, after in sorted(changes):
        print(f"{label}: {before} -> {after}", file=sys.stderr)
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)


def main():
    action, config_path, *args = sys.argv[1:]
    config = validate(json.loads(Path(config_path).read_text()))
    if action == "index":
        index(*args, config)
    elif action == "compatibility-policy":
        compatibility_policy(*args, config)
    elif action == "recommend":
        recommend_version(*args)
    elif action == "envoy-index":
        envoy_index(*args, config)
    elif action == "compatibility":
        check_chart_compatibility(*args)
    elif action == "upgrade-direction":
        upgrade_direction(*args)
    elif action == "check-apis":
        check_apis(*args)
    elif action == "archive":
        archive(*args, config)
    elif action == "render":
        render(config, args[0])
    elif action == "oci":
        ref = args[0].removeprefix("oci://")
        print("oci://" + registry_ref(ref, config))
    elif action == "envoy-proxy":
        resolve_envoy_proxy(args[0], args[1], args[2:], config)
    elif action == "values":
        values(args, config)
    elif action == "envoy-controller":
        print(envoy_controller(args[0], args[1:], config))
    elif action == "check-chart":
        chart = yaml_module().safe_load(sys.stdin)
        if (not isinstance(chart, dict) or chart.get("name") != args[0]
                or str(chart.get("version", "")).removeprefix("v") != args[1].removeprefix("v")):
            raise ValueError("Downloaded chart name/version does not match the request")
    elif action == "validate":
        print(config["profile"])
    else:
        raise ValueError("Unknown source operation")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[ERROR] " + str(exc), file=sys.stderr)
        raise SystemExit(1)
