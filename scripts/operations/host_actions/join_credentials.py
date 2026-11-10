# SPDX-License-Identifier: MIT
"""Short-lived worker join credentials, returned only on the private stdin channel."""
import re
import shlex

from ..configuration import ipv4, integer
from ..models import OperationError
from . import common


def issue(value):
    if not isinstance(value, dict) or set(value) != {"apiVIP", "apiPort"}:
        raise OperationError("ops.change.invalid_parameters")
    endpoint = ipv4(value["apiVIP"]) + ":" + str(integer(value["apiPort"], 1, 65535))
    _, output = common.run(
        [
            "kubeadm",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "token",
            "create",
            "--ttl=10m",
            "--print-join-command",
        ]
    )
    parts = shlex.split(output.strip())
    if (
        len(parts) != 7
        or parts[:3] != ["kubeadm", "join", endpoint]
        or parts[3] != "--token"
        or not re.fullmatch(r"[a-z0-9]{6}\.[a-z0-9]{16}", parts[4])
        or parts[5] != "--discovery-token-ca-cert-hash"
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", parts[6])
    ):
        raise OperationError("ops.lifecycle.join_state")
    return dict(token=parts[4], caHash=parts[6])


def revoke(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"id"}
        or not re.fullmatch(r"[a-z0-9]{6}", value["id"])
    ):
        raise OperationError("ops.change.invalid_parameters")
    common.run(
        ["kubeadm", "--kubeconfig=/etc/kubernetes/admin.conf", "token", "delete", value["id"]]
    )
    return dict(revoked=True)
