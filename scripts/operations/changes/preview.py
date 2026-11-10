# SPDX-License-Identifier: MIT
"""Render immutable intent through the registered capability's public projection."""


def show(plan, capability, translate):
    print(
        translate(
            "ops.change.preview",
            translate("ops.operation." + plan.capability.replace("-", "_")),
            plan.cluster_identity,
            plan.expires_at,
        )
    )
    for key, values in capability.preview(plan.parameters):
        print(translate(key, *values))
