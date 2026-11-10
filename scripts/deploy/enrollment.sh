#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Phase-two enrollment and authenticated SSH bootstrap. Definitions only.

setup_ssh() {
  [ "$SSH_READY" -eq 0 ] || return 0
  getent passwd "$SSH_USER" >/dev/null || die 'SSH user does not exist locally'
  HOME_DIR="$(getent passwd "$SSH_USER" | cut -d: -f6)"
  MESH_KEY="$HOME_DIR/.ssh/id_ed25519"
  [ -s "$MESH_KEY" ] && [ -s "$MESH_KEY.pub" ] || die 'Local node SSH key pair is missing; rerun phase 1'
  SSH_OPTS=(-o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes -i "$MESH_KEY")
  SSH_READY=1
}

atlas_deploy_enroll_configuration() {
  [ -n "$CONFIG_INPUT" ] && [ ! -f "$CONF" ] || return 0
  local node expected_code address secret actual result
  for node in "${CONFIG_NODES[@]}"; do
    [ "$node" != "$SELF" ] || continue
    address="${CONFIG_IP[$node]}"
    if actual="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$address" hostname -s 2>/dev/null)"; then
      [ "$actual" = "$node" ] || die "$(atlas_t phase02.config.conflict)"
      continue
    fi
    [ -t 0 ] || die "$(atlas_t phase02.config.enrollment_required)"
    while true; do
      printf '%s' "$(atlas_printf phase02.config.enroll_prompt "$node" "$address")" >&2
      IFS= read -r -s expected_code || return 1
      printf '\n' >&2
      if [[ "$expected_code" =~ ^([0-9a-fA-F]{12}|[0-9a-fA-F]{24})$ ]]; then
        secret="$expected_code"
      else
        result="$(decode_pairing_code "$expected_code")" || { atlas_t phase02.config.enrollment_retry >&2; printf '\n' >&2; continue; }
        IFS=$'\t' read -r actual secret <<< "$result"
        [ "$actual" = "$address" ] || die "$(atlas_t phase02.config.conflict)"
      fi
      unset expected_code
      if actual="$(enroll_node "$address" "$secret")"; then
        unset secret
        [ "$actual" = "$node" ] || die "$(atlas_t phase02.config.conflict)"
        break
      fi
      unset secret
      atlas_t phase02.config.enrollment_retry >&2; printf '\n' >&2
    done
  done
}

enroll_node() {
  local address="$1" code="$2" result name host_key
  [[ "$code" =~ ^([0-9a-fA-F]{12}|[0-9a-fA-F]{24})$ ]] || die "$address pairing code must be 12 hexadecimal characters (older 24-character codes also work)"
  result="$(printf '%s\n' "$code" | python3 "$PAIR_HELPER" enroll --address "$address" --code-stdin --public-key-file "$MESH_KEY.pub")" || return 1
  IFS=$'\t' read -r name host_key <<< "$result"
  install -d -m 700 /root/.ssh
  touch /root/.ssh/known_hosts
  chmod 600 /root/.ssh/known_hosts
  ssh-keygen -R "$address" -f /root/.ssh/known_hosts >/dev/null 2>&1 || true
  printf '%s %s\n' "$address" "$host_key" >> /root/.ssh/known_hosts
  printf '%s' "$name"
}
