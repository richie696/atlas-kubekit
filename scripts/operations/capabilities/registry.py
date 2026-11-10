# SPDX-License-Identifier: MIT
"""Finite capability registration; the execution core has no operation branches."""
from types import MappingProxyType

from . import lb_stats, security, registry_configure
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
    }
)
