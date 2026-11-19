# SPDX-License-Identifier: MIT
"""Finite capability registration; the execution core has no operation branches."""
from types import MappingProxyType

from . import lb_stats, security, registry_configure, monitoring_rules, monitoring_scrapes, monitoring_workloads, monitoring_storage
from . import monitoring_statefulsets, monitoring_daemonsets, monitoring_etcd, monitoring_longhorn, monitoring_longhorn_collection
from . import monitoring_longhorn_disk
from . import monitoring_email
from . import monitoring_email_diagnostics
from . import monitoring_relay_metrics
from . import monitoring_cilium
from . import monitoring_control_plane
from . import monitoring_dns
from . import monitoring_node_network
from . import monitoring_lb_collection
from . import monitoring_lb_rules
from . import monitoring_lb
from . import monitoring_robot
from . import monitoring_workload_health
from . import monitoring_certificates
from . import monitoring_network_probes
from . import monitoring_silence
from . import monitoring_inhibition
from . import monitoring_resource_remove
from . import monitoring_systemd
from . import monitoring_systemd_remove
from ..lifecycle import maintenance, upgrade, join, retire, replace
from ..components import cilium_upgrade
from ..protection import registration

CAPABILITIES = MappingProxyType(
    {
        "lb-stats": lb_stats,
        "node-maintenance": maintenance,
        "security-finalize": security,
        "cluster-upgrade": upgrade,
        "node-join": join,
        "node-retire": retire,
        "node-replace": replace,
        "registry-configure": registry_configure,
        "component-upgrade": cilium_upgrade,
        "protection-register": registration,
        "monitoring-node-rules": monitoring_rules,
        "monitoring-scrape-rules": monitoring_scrapes,
        "monitoring-workload-rules": monitoring_workloads,
        "monitoring-workload-health-rules": monitoring_workload_health,
        "monitoring-storage-rules": monitoring_storage,
        "monitoring-statefulset-rules": monitoring_statefulsets,
        "monitoring-daemonset-rules": monitoring_daemonsets,
        "monitoring-etcd-rules": monitoring_etcd,
        "monitoring-control-plane-rules": monitoring_control_plane,
        "monitoring-dns-rules": monitoring_dns,
        monitoring_node_network.NAME: monitoring_node_network,
        "monitoring-certificate-rules": monitoring_certificates,
        "monitoring-longhorn-rules": monitoring_longhorn,
        "monitoring-longhorn-disk-rules": monitoring_longhorn_disk,
        "monitoring-longhorn-collection": monitoring_longhorn_collection,
        "monitoring-lb-collection": monitoring_lb_collection,
        "monitoring-lb-rules": monitoring_lb_rules,
        "monitoring-lb": monitoring_lb,
        "monitoring-systemd-collection": monitoring_systemd,
        "monitoring-systemd-remove": monitoring_systemd_remove,
        "monitoring-email": monitoring_email,
        "monitoring-email-diagnostics": monitoring_email_diagnostics,
        "monitoring-robot": monitoring_robot,
        "monitoring-relay-metrics": monitoring_relay_metrics,
        "monitoring-cilium-collection": monitoring_cilium,
        "monitoring-network-probes": monitoring_network_probes,
        "monitoring-silence": monitoring_silence,
        "monitoring-inhibition": monitoring_inhibition,
        "monitoring-resource-remove": monitoring_resource_remove,
    }
)
