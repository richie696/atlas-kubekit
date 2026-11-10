#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Match the activated offline identity on every registered deployment node.

atlas_deploy_verify_materials() {
  local node observed expected=none
  if [ -f /var/lib/k8s-deploy/materials.json ]; then
    expected="$(python3 "$LIB/scripts/operations/materials/runtime.py" id)" \
      || die "$(atlas_t ops.materials.invalid)"
    python3 "$LIB/scripts/operations/materials/runtime.py" check-versions "$ATLAS_KUBERNETES_VERSION" "$ATLAS_CILIUM_VERSION" \
      || die "$(atlas_t ops.materials.invalid)"
  fi
  for node in "${NODES[@]}"; do
    observed="$(run "$node" 'if [ -f /var/lib/k8s-deploy/materials.json ]; then python3 /usr/local/lib/k8s-deploy/scripts/operations/materials/runtime.py id; else printf none; fi')" \
      || die "$(atlas_t ops.materials.invalid)"
    [ "$observed" = "$expected" ] || die "$(atlas_t ops.materials.incomplete)"
  done
}
