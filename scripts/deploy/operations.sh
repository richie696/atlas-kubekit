#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Public desired-state bridge. Sourcing defines functions only.

atlas_deploy_operations_runtime() {
  DEPLOY_OPERATIONS_TOOL="$HERE/scripts/operations/deployment_runtime.py"
  [ -r "$DEPLOY_OPERATIONS_TOOL" ] || DEPLOY_OPERATIONS_TOOL="$LIB/scripts/operations/deployment_runtime.py"
  [ -r "$DEPLOY_OPERATIONS_TOOL" ] || die "$(atlas_t phase02.config.missing_tool)"
  python3 "$DEPLOY_OPERATIONS_TOOL" install || die "$(atlas_t phase02.config.conflict)"
}

atlas_deploy_exit_report() {
  local status=$? report_status=0 outcome=failed
  [ "$status" -ne 0 ] || outcome="${DEPLOY_OUTCOME:-cancelled}"
  [ "$status" -ne 130 ] || outcome=cancelled
  rm -f -- "${HOSTS_FILE:-}" "${KEYS_FILE:-}"
  python3 "$LIB/scripts/operations/native_reports.py" --entrypoint 02 \
    --action deployment --result "$outcome" --lang "$ATLAS_LANG" || report_status=$?
  [ "$status" -ne 0 ] || status=$report_status
  trap - EXIT
  exit "$status"
}

atlas_deploy_local_lock() {
  [ ! -L "$STATE/deployment.lock" ] || die "$(atlas_t ops.error.path)"
  exec {DEPLOY_LOCAL_LOCK_FD}>"$STATE/deployment.lock"
  chmod 600 "$STATE/deployment.lock"
  flock -n "$DEPLOY_LOCAL_LOCK_FD" || die "$(atlas_t ops.error.locked)"
}

atlas_deploy_copy_operations_runtime() {
  local node="$1"
  [ "$node" != "$SELF" ] || return 0
  tar --exclude='__pycache__' -C "$LIB" -cf - scripts/operations scripts/lib VERSION \
    | remote "$node" "sudo -n tar -xf - -C $(q "$LIB")" \
    || die "$(atlas_t phase02.config.missing_tool)"
}

atlas_deploy_public_rows() {
  local key n
  for key in CLUSTER_NAME SSH_USER PROFILE VIP APP_INGRESS_VIP API_PORT VRRP_ID POD_CIDR SVC_CIDR LB_STATS_ALLOWED_CIDR LOCAL_REGISTRY_HOST; do
    printf '%s\t%s\n' "$key" "${!key}"
  done
  for n in "${NODES[@]}"; do
    printf 'NODE\t%s\t%s\t%s\t%s\t%s\n' "$n" "${IP[$n]}" "${NODE_IFACE[$n]}" "${PLAN_DISK[$n]:-}" "${ID[$n]:-}"
  done
}

atlas_deploy_shared_configuration() {
  local mode="$1" target="$SSH_USER@${IP[cp1]}"
  [ "$SELF" != cp1 ] || target=local
  atlas_deploy_public_rows | python3 "$DEPLOY_OPERATIONS_TOOL" "$mode" "$target" "$MESH_KEY" || die "$(atlas_t phase02.config.conflict)"
}

atlas_deploy_guard_existing_cluster() {
  local present managed
  present="$(run cp1 'if test -s /etc/kubernetes/admin.conf; then printf yes; else printf no; fi')" \
    || die "$(atlas_t phase02.config.conflict)"
  [ "$present" = yes ] || return 0
  [ -f "$CONF" ] || die "$(atlas_t phase02.error.existing_cluster_plan)"
  if [ -z "$CONFIG_INPUT" ] && ! grep -q '^CLUSTER_NAME=' "$CONF"; then
    CLUSTER_NAME="$(atlas_deploy_shared_configuration name)" || die "$(atlas_t phase02.config.conflict)"
  fi
  atlas_deploy_shared_configuration guard
  managed="$(atlas_deploy_shared_configuration managed)" || die "$(atlas_t phase02.config.conflict)"
  if [ "$managed" = yes ] && [ -f "$STATE/cluster-ready" ]; then
    say "$(atlas_t phase02.progress.verify_existing)"
    atlas_deploy_verify_cluster
    DEPLOY_OUTCOME=passed
    say "$(atlas_t phase02.progress.complete)"
    exit 0
  fi
  atlas_deploy_lock_begin
}

atlas_deploy_lock_begin() {
  [ "${DEPLOY_CLUSTER_LOCK_ACTIVE:-0}" -eq 0 ] || return 0
  atlas_deploy_shared_configuration lock-begin
  DEPLOY_CLUSTER_LOCK_ACTIVE=1
}

atlas_deploy_lock_fence() {
  [ "${DEPLOY_CLUSTER_LOCK_ACTIVE:-0}" -eq 1 ] || return 0
  atlas_deploy_shared_configuration lock-fence
}

atlas_deploy_lock_complete() {
  [ "${DEPLOY_CLUSTER_LOCK_ACTIVE:-0}" -eq 1 ] || die "$(atlas_t ops.change.lock_lost)"
  atlas_deploy_shared_configuration lock-complete
  DEPLOY_CLUSTER_LOCK_ACTIVE=0
}
