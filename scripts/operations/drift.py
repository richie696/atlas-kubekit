# SPDX-License-Identifier: MIT
"""Compare supplied desired/observed values without querying or mutating nodes."""
import json

from .configuration import normalize, revision
from .models import Finding, OperationPlan, fingerprint, service_findings


def display(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded) <= 512 else encoded[:509] + "..."


def compare(check, expected, actual):
    return Finding(
        check,
        "PASS" if actual == expected else "FAIL",
        "ops.config.compare",
        (check, display(expected), display(actual)),
    )


def findings(config, observations):
    data = normalize(config)
    rows = []
    cluster = data["cluster"]
    cps = [n for n in data["nodes"] if n["name"].startswith("cp")]
    lbs = [n for n in data["nodes"] if n["name"].startswith("lb")]
    for node in data["nodes"]:
        name = node["name"]
        facts = observations.get(name, {})
        if facts.get("schemaVersion") != 1:
            rows.append(Finding(name + ".collection", "UNVERIFIED", "ops.finding.unverified"))
            continue
        role = "lb" if name.startswith("lb") else "cp" if name.startswith("cp") else "worker"
        if data.get('registry') is not None and role!='lb':
            registry=facts.get('registry',{})
            if registry.get('state')!='ok':
                rows.append(Finding(name+'.registry','UNVERIFIED','ops.finding.unverified'))
            else:
                current=next((row for row in registry.get('entries',[]) if row.get('authority')==data['registry']['authority']),None)
                rows.append(compare(name+'.registry',data['registry'],current))
        rows += [
            compare(name + ".hostname", name, facts.get("hostname")),
            compare(name + ".role", role, facts.get("role")),
        ]
        addresses = facts.get("addresses", {})
        if addresses.get("state") == "ok":
            present = dict(interface=node["interface"], address=node["address"]) in addresses.get(
                "values", []
            )
            rows.append(compare(name + ".address-interface", True, present))
        else:
            rows.append(
                Finding(name + ".address-interface", "UNVERIFIED", "ops.finding.unverified")
            )
        pin = node["identityFingerprint"]
        if facts.get("identityState") != "ok":
            rows.append(Finding(name + ".identity", "UNVERIFIED", "ops.finding.unverified"))
        elif pin:
            rows.append(compare(name + ".identity", pin, facts.get("identityFingerprint")))
        else:
            rows.append(
                Finding(
                    name + ".identity",
                    "WARN",
                    "ops.config.pin_missing",
                    (name, facts.get("identityFingerprint", "unknown")),
                )
            )
        rows += [
            Finding(name + "." + r.check, r.status, r.message_key, r.values)
            for r in service_findings({**facts, "role": role})
        ]
        saved = facts.get("deployment", {})
        for field, key in [
            ("PROFILE", "profile"),
            ("LOCAL_REGISTRY_HOST", "localRegistryHost"),
            ("SSH_USER", None),
        ]:
            expected = data["ssh"]["user"] if key is None else cluster[key]
            rows.append(
                compare(name + ".saved-" + field, expected, saved["values"][field])
                if saved.get("state") == "ok" and field in saved.get("values", {})
                else Finding(name + ".saved-" + field, "UNVERIFIED", "ops.finding.unverified")
            )
        if node["dataDevice"]:
            rows.append(
                Finding(name + ".data-device", "UNVERIFIED", "ops.config.storage_unverified")
            )
        if cluster["localRegistryHost"] and role != "lb":
            rows.append(
                Finding(name + ".registry-runtime", "UNVERIFIED", "ops.config.registry_unverified")
            )
        if role != "lb":
            version = facts.get("versions", {}).get("kubernetes")
            rows.append(
                compare(name + ".kubelet-version", data["versions"]["kubernetes"], version)
                if version
                else Finding(name + ".kubelet-version", "UNVERIFIED", "ops.finding.unverified")
            )
        if role == "cp":
            version = facts.get("versions", {}).get("cilium")
            rows.append(
                compare(name + ".cilium-version", data["versions"]["cilium"], version)
                if version
                else Finding(name + ".cilium-version", "UNVERIFIED", "ops.finding.unverified")
            )
            network = facts.get("network", {})
            if network.get("state") == "ok":
                for key, field in [("podSubnet", "podCIDR"), ("serviceSubnet", "serviceCIDR")]:
                    rows.append(
                        compare(
                            name + "." + field, cluster[field], network.get("values", {}).get(key)
                        )
                    )
            else:
                rows.append(Finding(name + ".network", "UNVERIFIED", "ops.finding.unverified"))
        if role == "lb":
            actual = facts.get("lb", {})
            if actual.get("state") != "ok":
                rows.append(
                    Finding(name + ".lb-configuration", "UNVERIFIED", "ops.finding.unverified")
                )
                continue
            for key in ("apiPort", "vrrpID"):
                rows.append(compare(name + "." + key, cluster[key], actual.get(key)))
            rows.append(compare(name + ".lb-interface", node["interface"], actual.get("interface")))
            for key in ("apiVIP", "appVIP"):
                if cluster[key]:
                    rows.append(
                        compare(
                            name + ".configured-" + key,
                            True,
                            cluster[key] in actual.get("vips", []),
                        )
                    )
            wanted = sorted((n["name"], n["address"], cluster["apiPort"]) for n in cps)
            observed = sorted(
                (n.get("name"), n.get("address"), n.get("port")) for n in actual.get("backends", [])
            )
            rows.append(compare(name + ".api-backends", wanted, observed))
            policy = "cidr" if cluster["statsAllowedCIDR"] else "loopback"
            if actual.get("statsPolicy") not in ("loopback", "cidr", "unrestricted"):
                rows.append(Finding(name + ".stats-policy", "UNVERIFIED", "ops.finding.unverified"))
            else:
                rows.append(compare(name + ".stats-policy", policy, actual.get("statsPolicy")))
                if policy == "cidr":
                    rows.append(
                        compare(
                            name + ".stats-cidr",
                            [cluster["statsAllowedCIDR"]],
                            actual.get("statsCIDRs"),
                        )
                    )
    owners = {}
    for key in ("apiVIP", "appVIP"):
        if not cluster[key]:
            continue
        certain = all(
            (f := observations.get(n["name"], {})).get("hostname") == n["name"]
            and f.get("role") == "lb"
            and f.get("identityState") == "ok"
            and f.get("addresses", {}).get("state") == "ok"
            and (
                not n["identityFingerprint"]
                or f.get("identityFingerprint") == n["identityFingerprint"]
            )
            for n in lbs
        )
        if not certain:
            rows.append(
                Finding("cluster." + key + "-owner", "UNVERIFIED", "ops.finding.unverified")
            )
            continue
        holders = [
            n["name"]
            for n in lbs
            if any(
                a.get("address") == cluster[key]
                for a in observations[n["name"]]["addresses"].get("values", [])
            )
        ]
        rows.append(compare("cluster." + key + "-owner-count", 1, len(holders)))
        if len(holders) == 1:
            owners[key] = holders[0]
    if set(owners) == {"apiVIP", "appVIP"}:
        rows.append(compare("cluster.vip-owner-alignment", owners["apiVIP"], owners["appVIP"]))
    return tuple(rows)


def plan(config, observations):
    data = normalize(config)
    return OperationPlan(
        "configuration-review",
        fingerprint(dict(config=data, observations=observations)),
        ("review-differences", "revalidate-identities", "use-capability-specific-confirmation"),
        scope="cluster",
        resources=tuple(n["name"] for n in data["nodes"]),
        parameters=(("configRevision", revision(data)),),
    )
