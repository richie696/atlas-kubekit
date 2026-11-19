# SPDX-License-Identifier: MIT
"""DaemonSet rule capability bound to the shared controller protocol."""
from functools import partial
from . import monitoring_controllers as common
parameters = partial(common.parameters, "daemonset")
from_arguments = partial(common.from_arguments, "daemonset")
preview = partial(common.preview, "daemonset")
observe = partial(common.observe, "daemonset")
apply = partial(common.apply, "daemonset")
