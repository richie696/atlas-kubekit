# SPDX-License-Identifier: MIT
"""StatefulSet rule capability bound to the shared controller protocol."""
from functools import partial
from . import monitoring_controllers as common
parameters = partial(common.parameters, "statefulset")
from_arguments = partial(common.from_arguments, "statefulset")
preview = partial(common.preview, "statefulset")
observe = partial(common.observe, "statefulset")
apply = partial(common.apply, "statefulset")
