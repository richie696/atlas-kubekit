#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Source-only bridge: data is parsed, never executed as shell assignments.

atlas_versions_load() {
  local directory="$1" rows key value
  rows="$(python3 "$directory/component_versions.py")" || return 1
  while IFS=$'\t' read -r key value; do
    case "$key" in
      ATLAS_KUBERNETES_VERSION) ATLAS_KUBERNETES_VERSION="$value" ;;
      ATLAS_CILIUM_VERSION) ATLAS_CILIUM_VERSION="$value" ;;
      ATLAS_HELM_VERSION) ATLAS_HELM_VERSION="$value" ;;
      ATLAS_CRI_TOOLS_VERSION) ATLAS_CRI_TOOLS_VERSION="$value" ;;
      *) return 1 ;;
    esac
  done <<< "$rows"
  [ -n "${ATLAS_KUBERNETES_VERSION:-}" ] && [ -n "${ATLAS_CILIUM_VERSION:-}" ] \
    && [ -n "${ATLAS_HELM_VERSION:-}" ] && [ -n "${ATLAS_CRI_TOOLS_VERSION:-}" ]
}
