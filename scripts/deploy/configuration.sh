#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Phase-two JSON bridge and identity policy. Sourcing defines functions only.

atlas_deploy_config_load() {
  if [ "$CONFIG_ARGUMENT_ERROR" -eq 1 ] || { [ "$CONFIG_OPTION_SEEN" -eq 1 ] && [ -z "$CONFIG_INPUT" ]; }; then
    die "$(atlas_t phase02.config.invalid)"
  fi
  CONFIG_ROWS="" CONFIG_IDENTITY_SOURCE=""
  if [ -n "$CONFIG_INPUT" ]; then
    CONFIG_TOOL="$HERE/scripts/operations/deployment_config.py"
    [ -r "$CONFIG_TOOL" ] || die "$(atlas_t phase02.config.missing_tool)"
    CONFIG_ROWS="$(python3 "$CONFIG_TOOL" "$CONFIG_INPUT")" || die "$(atlas_t phase02.config.invalid)"
    CONFIG_IDENTITY_SOURCE="$(python3 "$CONFIG_TOOL" --identity-source)" || die "$(atlas_t phase02.config.invalid)"
    if [ "$CONFIG_CHECK" -eq 1 ]; then
      [ "$#" -eq 0 ] || die "$(atlas_t phase02.config.invalid)"
      atlas_t phase02.config.valid; printf '\n'; exit 0
    fi
    case "${1:-}" in ''|--resume|--help|-h) ;; *) die "$(atlas_t phase02.config.invalid)";; esac
  fi
  CONFIG_NODES=()
  if [ -n "$CONFIG_ROWS" ]; then
    while IFS=$'\t' read -r field value address interface disk pin extra; do
      case "$field" in
        NODE)
          CONFIG_NODES+=("$value"); CONFIG_IP[$value]="$address"; CONFIG_IFACE[$value]="$interface"
          CONFIG_DISK[$value]="$disk"; CONFIG_PIN[$value]="$pin"
          ;;
        CLUSTER_NAME|SSH_USER|PROFILE|VIP|APP_INGRESS_VIP|API_PORT|VRRP_ID|POD_CIDR|SVC_CIDR|LB_STATS_ALLOWED_CIDR|LOCAL_REGISTRY_HOST)
          CONFIG_VALUES[$field]="$value"
          ;;
        *) die "$(atlas_t phase02.config.invalid)" ;;
      esac
    done <<< "$CONFIG_ROWS"
  fi
}

atlas_deploy_validate_plan() {
  [ "${#NODES[@]}" -eq "${#CONFIG_NODES[@]}" ] || die "$(atlas_t phase02.config.conflict)"
  local key n
  for key in CLUSTER_NAME SSH_USER PROFILE VIP APP_INGRESS_VIP API_PORT VRRP_ID POD_CIDR SVC_CIDR LB_STATS_ALLOWED_CIDR LOCAL_REGISTRY_HOST; do
    [ "${!key}" = "${CONFIG_VALUES[$key]}" ] || die "$(atlas_t phase02.config.conflict)"
  done
  for n in "${CONFIG_NODES[@]}"; do
    [ "${IP[$n]:-}" = "${CONFIG_IP[$n]}" ] || die "$(atlas_t phase02.config.conflict)"
    [ -z "${NODE_IFACE[$n]:-}" ] || [ "${NODE_IFACE[$n]}" = "${CONFIG_IFACE[$n]}" ] || die "$(atlas_t phase02.config.conflict)"
    [[ "$n" = lb* ]] || [ "${CONFIG_DISK[$n]}" = - ] || [ "${DISK[$n]:-}" = "${CONFIG_DISK[$n]}" ] || die "$(atlas_t phase02.config.conflict)"
  done
}
atlas_deploy_persist_identity() {
  [ -n "$NODE_IDENTITY_SOURCE" ] || return 0
  local candidate n
  candidate="$(mktemp "$STATE/.config-identity.XXXXXXXX")" || die "$(atlas_t phase02.config.conflict)"
  chmod 600 "$candidate"
  if ! (
    sed '/^# BEGIN ATLAS CONFIG IDENTITY$/,/^# END ATLAS CONFIG IDENTITY$/d' "$CONF" || exit 1
    printf '# BEGIN ATLAS CONFIG IDENTITY\nNODE_IDENTITY_SOURCE=%q\n' "$NODE_IDENTITY_SOURCE" || exit 1
    for n in "${NODES[@]}"; do
      [ -z "${NODE_IDENTITY_PIN[$n]:-}" ] || printf 'NODE_IDENTITY_PIN[%q]=%q\n' "$n" "${NODE_IDENTITY_PIN[$n]}" || exit 1
    done
    printf '# END ATLAS CONFIG IDENTITY\n' || exit 1
  ) > "$candidate"; then
    rm -f "$candidate"; die "$(atlas_t phase02.config.conflict)"
  fi
  if cmp -s "$candidate" "$CONF"; then rm -f "$candidate";
  elif ! mv -f "$candidate" "$CONF"; then rm -f "$candidate"; die "$(atlas_t phase02.config.conflict)"; fi
}

atlas_deploy_persist_name() {
  [ -f "$CONF" ] || return 0
  grep -q '^CLUSTER_NAME=' "$CONF" && return 0
  local candidate
  candidate="$(mktemp "$STATE/.cluster-name.XXXXXXXX")" || die "$(atlas_t phase02.config.conflict)"
  chmod 600 "$candidate"
  if ! { cat "$CONF" && printf 'CLUSTER_NAME=%q\n' "$CLUSTER_NAME"; } > "$candidate"; then
    rm -f "$candidate"; die "$(atlas_t phase02.config.conflict)"
  fi
  if ! mv -f "$candidate" "$CONF"; then
    rm -f "$candidate"; die "$(atlas_t phase02.config.conflict)"
  fi
}
