#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Shared dependency initialization. Sourcing has no system effects.

atlas_stage_packages_init() {
  local directory="$1"
  [ -r "$directory/packages.sh" ] && [ -r "$directory/i18n.sh" ] || return 1
  # shellcheck source=scripts/lib/i18n.sh
  source "$directory/i18n.sh"
  # shellcheck source=scripts/lib/packages.sh
  source "$directory/packages.sh"
  atlas_i18n_init "${ATLAS_LANG:-en}"
}
