#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fixed native stage transport; version policy cannot drift through exported defaults.

atlas_deploy_stage() {
  atlas_deploy_lock_fence
  local node="$1" script="$2" extra="${3:-}"
  shift 3 || return 1
  local command argument kubernetes="$ATLAS_KUBERNETES_VERSION"
  [ "$script" != setup-k8s-node.sh ] || kubernetes="${kubernetes#v}"
  command="env ATLAS_LANG=$(q "$ATLAS_LANG") ATLAS_LOCALES_DIR=$(q "$LIB/locales")"
  command+=" VIP=$(q "$VIP") API_PORT=$(q "$API_PORT") IFACE=$(q "${NODE_IFACE[$node]}")"
  command+=" CP1=$(q "${IP[cp1]}") POD_CIDR=$(q "$POD_CIDR") SVC_CIDR=$(q "$SVC_CIDR") VRRP_ID=$(q "$VRRP_ID")"
  command+=" K8S_VERSION=$(q "$kubernetes") K8S_DEB_VERSION=$(q "${ATLAS_KUBERNETES_VERSION#v}-1.1")"
  command+=" CILIUM_VERSION=$(q "$ATLAS_CILIUM_VERSION") HELM_VERSION=$(q "$ATLAS_HELM_VERSION")"
  command+=" CRI_TOOLS_DEB_VERSION=$(q "${ATLAS_CRI_TOOLS_VERSION#v}-1.1") K8S_TEST_MODE=$(q "$TEST_MODE")"
  # extra contains only coordinator-built, quoted assignments; never configuration text.
  command+=" $extra python3 $(q "$LIB/scripts/operations/native_stage.py") $(q "$script")"
  for argument in "$@"; do command+=" $(q "$argument")"; done
  run "$node" "$command"
}
