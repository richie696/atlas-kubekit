#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Network readiness is independent of whether delivery material is online.

atlas_prepare_network_ready() {
  if [ -f /var/lib/k8s-deploy/materials.json ]; then
    python3 /usr/local/lib/k8s-deploy/scripts/operations/materials/runtime.py id >/dev/null || return 1
    atlas_t phase01.message.offline_dns; printf '\n'
    return 0
  fi
  getent hosts pkgs.k8s.io >/dev/null
}
