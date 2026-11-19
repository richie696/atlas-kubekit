# SPDX-License-Identifier: MIT
"""Render safe defaults for the optional Blackbox Exporter add-on."""
import sys
import re


def values(release="kube-prometheus-stack"):
    """Configure one secure exporter per node; target monitors are managed by 06."""
    if not isinstance(release, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", release):
        raise ValueError("invalid Prometheus release label")
    return (
        "kind: DaemonSet\n"
        "automountServiceAccountToken: false\n"
        "serviceMonitor:\n"
        "  enabled: false\n"
        "  selfMonitor:\n"
        "    enabled: false\n"
        "config:\n"
        "  modules:\n"
        "    http_2xx:\n"
        "      prober: http\n"
        "      timeout: 5s\n"
        "      http:\n"
        "        follow_redirects: false\n"
        "        preferred_ip_protocol: ip4\n"
        "podSecurityContext:\n"
        "  seccompProfile:\n"
        "    type: RuntimeDefault\n"
        "securityContext:\n"
        "  runAsUser: 1000\n"
        "  runAsGroup: 1000\n"
        "  runAsNonRoot: true\n"
        "  readOnlyRootFilesystem: true\n"
        "  allowPrivilegeEscalation: false\n"
        "  capabilities:\n"
        "    drop: [ALL]\n"
        "resources:\n"
        "  requests:\n"
        "    cpu: 25m\n"
        "    memory: 32Mi\n"
        "  limits:\n"
        "    memory: 128Mi\n"
        "tolerations:\n"
        "  - operator: Exists\n"
        "    effect: NoSchedule\n"
        "  - operator: Exists\n"
        "    effect: NoExecute\n"
        "pod:\n"
        "  labels:\n"
        "    atlas-kubekit.io/role: network-probe\n"
        "service:\n"
        "  labels:\n"
        "    release: " + release + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1:] != ["--safe-defaults"]:
        raise SystemExit("usage: blackbox_values.py --safe-defaults")
    sys.stdout.write(values())
