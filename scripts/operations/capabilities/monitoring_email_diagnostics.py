# SPDX-License-Identifier: MIT
"""Plan an SMTP notification route through the diagnostic relay."""
from copy import copy

from . import monitoring_robot as relay_capability


def parameters(value):
    return relay_capability.parameters(value)


def from_arguments(args, config, environment):
    routed = copy(args)
    routed.monitoring_robot_kind = "email"
    routed.monitoring_robot_receiver = args.monitoring_email_receiver
    routed.monitoring_robot_secret = args.monitoring_email_secret
    routed.monitoring_robot_endpoint_key = args.monitoring_email_secret_key
    routed.monitoring_robot_signing_key = "-"
    return relay_capability.from_arguments(routed, config, environment)


def preview(value):
    return relay_capability.preview(value)


def observe(value, environment):
    return relay_capability.observe(value, environment)


def apply(value, environment, checkpoint, run_id):
    return relay_capability.apply(value, environment, checkpoint, run_id)
