#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Atlas KubeKit | By Atlas Richie
# Phase 2: orchestrate the nodes registered at the first interactive run.
set -euo pipefail
NODES=() LBS=() CPS=() WORKERS=() K8S_NODES=() JOIN_NODES=()
STAGES=(setup-k8s-node.sh setup-lb.sh configure-registry-mirror.sh init-cluster.sh install-cilium.sh create-join-credentials.sh join-nodes.sh configure-coredns-ha.sh)
STATE=/var/lib/k8s-deploy
LIB=/usr/local/lib/k8s-deploy
CONF=$STATE/cluster.conf
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repository packages keep internal stages separate; installed/legacy copies stay flat.
STAGE_DIR="$HERE/scripts/stages"
[ -d "$STAGE_DIR" ] || STAGE_DIR="$HERE"
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
ask() { local answer; read -r -p "$1 [$2]: " answer; printf '%s' "${answer:-$2}"; }
q() { printf '%q' "$1"; }
valid_ip() { local p; local -a a; [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1; IFS=. read -r -a a <<< "$1"; for p in "${a[@]}"; do [ "$p" -le 255 ] || return 1; done; }
decode_pairing_code() {
  K8S_PAIR_CODE="$1" python3 - <<'PY'
import ipaddress
import os

alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
code = os.environ['K8S_PAIR_CODE'].upper().replace('-', '').replace('O', '0').replace('I', '1').replace('L', '1')
if len(code) != 16 or any(char not in alphabet for char in code):
    raise SystemExit('Enter a 16-character pairing code or a private IPv4 address')
value = 0
for char in code:
    value = value * 32 + alphabet.index(char)
packed = value.to_bytes(10, 'big')
print(f'{ipaddress.IPv4Address(packed[:4])}\t{packed[4:].hex()}')
PY
}
role() { case "$1" in lb*) echo lb;; cp*) echo cp;; w*) echo worker;; esac; }
rebuild_groups() {
  local n
  LBS=() CPS=() WORKERS=()
  for n in "${NODES[@]}"; do
    [[ "$n" =~ ^(lb|cp|w)[1-9][0-9]*$ ]] || die "Invalid registered hostname: $n"
    case "$n" in lb*) LBS+=("$n");; cp*) CPS+=("$n");; w*) WORKERS+=("$n");; esac
  done
  if ((${#LBS[@]})); then mapfile -t LBS < <(printf '%s\n' "${LBS[@]}" | sort -V); fi
  if ((${#CPS[@]})); then mapfile -t CPS < <(printf '%s\n' "${CPS[@]}" | sort -V); fi
  if ((${#WORKERS[@]})); then mapfile -t WORKERS < <(printf '%s\n' "${WORKERS[@]}" | sort -V); fi
  NODES=("${LBS[@]}" "${CPS[@]}" "${WORKERS[@]}")
  K8S_NODES=("${CPS[@]}" "${WORKERS[@]}")
  JOIN_NODES=("${CPS[@]:1}" "${WORKERS[@]}")
}
validate_inventory() {
  [ "${#LBS[@]}" -ge 1 ] && [ "${#CPS[@]}" -ge 1 ] || die 'Register at least one LB and one control-plane node'
  [ "${#LBS[@]}" -le 250 ] || die 'Keepalived priorities support at most 250 load balancers'
  [ "${LBS[0]}" = lb1 ] && [ "${CPS[0]}" = cp1 ] || die 'The inventory must include lb1 and cp1 as bootstrap nodes'
  [[ " ${NODES[*]} " == *" $SELF "* ]] || die "Local node $SELF is absent from the inventory"
}
case "${1:-}" in --help|-h) echo 'Atlas KubeKit | By Atlas Richie'; echo 'Usage: sudo bash 02-deploy-cluster.sh [--resume|--status]'; exit 0;; --status) [ -f "$STATE/cluster-ready" ] && echo 'Deployment complete' || echo 'Deployment incomplete'; exit 0;; ''|--resume) ;; *) die 'Unknown argument';; esac
if [ -t 0 ]; then say 'Atlas KubeKit | By Atlas Richie | Phase 2: cluster deployment'; fi
[ "$(id -u)" -eq 0 ] || die 'Run this script with sudo'
[ -f "$STATE/ready" ] || die 'Phase 1 is incomplete on this node; run 01-prepare-node.sh first'
SELF="$(hostname -s)"
[[ "$SELF" =~ ^(lb|cp|w)[1-9][0-9]*$ ]] || die 'Local hostname must match lbN/cpN/wN'
[ "${BASH_VERSINFO[0]}" -ge 4 ] || die 'Bash 4 or newer is required'
for f in "${STAGES[@]}"; do [ -f "$STAGE_DIR/$f" ] || die "Missing $STAGE_DIR/$f; copy the complete deployment package"; done
declare -A IP DISK ID UUID MAC PUB HOSTKEY NODE_IFACE NODE_DATA NODE_HOME
SSH_USER="$(awk -F= '$1=="SSH_USER" {print $2; exit}' "$STATE/node.conf")"
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] && [ "$SSH_USER" != root ] || die 'Local phase-1 SSH user is invalid'
VIP=10.20.1.9 API_PORT=6443 VRRP_ID=51 POD_CIDR=10.244.0.0/16 SVC_CIDR=10.96.0.0/12
PROFILE=standard
SSH_READY=0
setup_ssh() {
  [ "$SSH_READY" -eq 0 ] || return 0
  getent passwd "$SSH_USER" >/dev/null || die 'SSH user does not exist locally'
  HOME_DIR="$(getent passwd "$SSH_USER" | cut -d: -f6)"
  MESH_KEY="$HOME_DIR/.ssh/id_ed25519"
  [ -s "$MESH_KEY" ] && [ -s "$MESH_KEY.pub" ] || die 'Local node SSH key pair is missing; rerun phase 1'
  SSH_OPTS=(-o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i "$MESH_KEY")
  SSH_READY=1
}
enroll_node() {
  local address="$1" code="$2" result name host_key
  [[ "$code" =~ ^([0-9a-fA-F]{12}|[0-9a-fA-F]{24})$ ]] || die "$address pairing code must be 12 hexadecimal characters (older 24-character codes also work)"
  result="$(K8S_ENROLL_ADDRESS="$address" K8S_ENROLL_CODE="${code,,}" K8S_ENROLL_PUB="$(cat "$MESH_KEY.pub")" python3 - <<'PY'
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request

address = os.environ['K8S_ENROLL_ADDRESS']
secret = bytes.fromhex(os.environ['K8S_ENROLL_CODE'])
key = os.environ['K8S_ENROLL_PUB'].strip()
nonce = secrets.token_hex(16)
mac = hmac.new(secret, (nonce + '\n' + key).encode(), hashlib.sha256).hexdigest()
request = urllib.request.Request(
    'http://' + address + ':25422/enroll',
    data=json.dumps({'nonce': nonce, 'key': key, 'mac': mac}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
try:
    with urllib.request.urlopen(request, timeout=8) as response:
        reply = json.load(response)
except urllib.error.HTTPError as exc:
    if exc.code == 401:
        raise SystemExit('Invalid pairing code; check the current code on the target with 01-prepare-node.sh --code') from None
    if exc.code == 403:
        raise SystemExit('Pairing rejected by an older enrollment service; check its current code with 01-prepare-node.sh --code') from None
    raise SystemExit(f'Enrollment service returned HTTP {exc.code}') from None
except urllib.error.URLError as exc:
    raise SystemExit(f'Cannot reach the enrollment service: {exc.reason}') from None
name, host_key = reply['name'], reply['host_key']
expected = hmac.new(secret, ('OK\n' + nonce + '\n' + name + '\n' + host_key).encode(), hashlib.sha256).hexdigest()
if reply['nonce'] != nonce or not hmac.compare_digest(expected, reply['mac']):
    raise SystemExit('Enrollment response authentication failed')
if not re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', name):
    raise SystemExit('Invalid enrolled hostname')
if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', host_key):
    raise SystemExit('Invalid enrolled host key')
print(name + '\t' + host_key)
PY
)" || return 1
  IFS=$'\t' read -r name host_key <<< "$result"
  install -d -m 700 /root/.ssh
  touch /root/.ssh/known_hosts
  chmod 600 /root/.ssh/known_hosts
  ssh-keygen -R "$address" -f /root/.ssh/known_hosts >/dev/null 2>&1 || true
  printf '%s %s\n' "$address" "$host_key" >> /root/.ssh/known_hosts
  printf '%s' "$name"
}
read_manifest() {
  local address="$1" content="$2" line key value n
  local -A fields=()
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" == *=* ]] || die "$address has a malformed node manifest"
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in VERSION|PHASE1_READY|NAME|ROLE|IP|PREFIX|IFACE|NETWORK_MODE|SSH_USER|SSH_HOME|NODE_PUBLIC_KEY|DATA_DEVICE|OS_ID|OS_VERSION|ARCH) ;; *) die "$address has an unknown node manifest field: $key";; esac
    [ -z "${fields[$key]+set}" ] || die "$address has a duplicate node manifest field: $key"
    fields[$key]="$value"
  done <<< "$content"
  [ "${fields[VERSION]:-}" = 1 ] && [ "${fields[OS_ID]:-}" = ubuntu ] && [ "${fields[OS_VERSION]:-}" = 24.04 ] && [ "${fields[ARCH]:-}" = amd64 ] || die "$address has an unsupported node manifest"
  [ "${fields[PHASE1_READY]:-1}" = 1 ] || die "$address has only a planned configuration; complete phase 1 before deployment"
  n="${fields[NAME]:-}"
  [[ "$n" =~ ^(lb|cp|w)[1-9][0-9]*$ ]] || die "$address has an invalid hostname in its manifest"
  [ "${fields[ROLE]:-}" = "$(role "$n")" ] || die "$address has a mismatched node role"
  [ "${fields[SSH_USER]:-}" = "$SSH_USER" ] || die "$address uses a different SSH administrator"
  [[ "${fields[SSH_HOME]:-}" =~ ^/[a-zA-Z0-9._/-]+$ ]] || die "$address has an invalid SSH home directory"
  [ "${fields[IP]:-}" = "$address" ] || die "$n manifest IP does not match the SSH address $address"
  valid_ip "$address" || die "$n has an invalid IP"
  [[ "${fields[PREFIX]:-}" =~ ^[0-9]+$ ]] && [ "${fields[PREFIX]}" -ge 1 ] && [ "${fields[PREFIX]}" -le 32 ] || die "$n has an invalid prefix"
  [[ "${fields[IFACE]:-}" =~ ^[a-zA-Z0-9_.:-]+$ ]] || die "$n has an invalid network interface"
  case "${fields[NETWORK_MODE]:-}" in keep|static) ;; *) die "$n has an invalid network mode";; esac
  [[ "${fields[NODE_PUBLIC_KEY]:-}" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die "$n has an invalid SSH public key"
  [ -z "${fields[DATA_DEVICE]:-}" ] || [[ "${fields[DATA_DEVICE]}" =~ ^/dev/[a-zA-Z0-9._/-]+$ ]] || die "$n has an invalid data-disk path"
  [ -n "${IP[$n]+set}" ] && [ "${IP[$n]}" = "$address" ] || die "$address manifest hostname does not match the registered node"
  [ "${NODE_HOME[$n]}" = "${fields[SSH_HOME]}" ] || die "$n manifest SSH home does not match the current account"
  [ "${PUB[$n]}" = "${fields[NODE_PUBLIC_KEY]}" ] || die "$n manifest SSH public key does not match the current key"
  NODE_IFACE[$n]="${fields[IFACE]}"
  NODE_DATA[$n]="${fields[DATA_DEVICE]:-}"
  printf '  %-8s %-8s %s via %s\n' "$n" "${fields[ROLE]}" "$address" "${fields[IFACE]}"
}
fetch_manifest() {
  local n="$1" content
  if [ "$n" = "$SELF" ]; then
    content="$(cat "$HOME_DIR/.k8s-deploy/node.conf")" || die 'Local phase-1 manifest is missing; run 01-prepare-node.sh --config-only'
  else
    content="$(sudo -H -u "$SSH_USER" ssh -i "$MESH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 "$SSH_USER@$n" 'cat ~/.k8s-deploy/node.conf')" || die "$n manifest is unavailable over the SSH mesh; run phase 1 --config-only there"
  fi
  read_manifest "${IP[$n]}" "$content"
}
if [ -f "$CONF" ]; then
  [ "$(stat -c %u "$CONF")" = 0 ] && [ "$(stat -c %a "$CONF")" = 600 ] || die 'cluster.conf owner or permissions are unsafe'
  # Generated here, owned by root and mode 0600.
  . "$CONF"
  # Earlier cluster.conf files did not persist NODES and always used this topology.
  if ((${#NODES[@]} == 0)); then NODES=(lb1 lb2 cp1 cp2 cp3 w1 w2 w3); fi
else
  [ -t 0 ] && [ "${1:-}" != --resume ] || die 'The first run requires an interactive terminal'
  setup_ssh
  LOCAL_IP="$(awk -F= '$1=="IP" {print $2; exit}' "$STATE/node.conf")"
  valid_ip "$LOCAL_IP" || die 'Local phase-1 IP is invalid'
  IP[$SELF]="$LOCAL_IP"
  NODES+=("$SELF")
  say 'Register remaining nodes by pairing code or reachable private IP. Enter done when the list is complete.'
  while true; do
    read -r -p 'Node pairing code / private IPv4 (or done): ' candidate
    [ "${candidate,,}" = done ] && break
    bundled=0
    if ! valid_ip "$candidate"; then
      if ! decoded="$(decode_pairing_code "$candidate")"; then continue; fi
      IFS=$'\t' read -r candidate enrollment_code <<< "$decoded"
      bundled=1
      printf '  Pairing code targets %s\n' "$candidate"
    fi
    duplicate=0
    for existing in "${NODES[@]}"; do [ "${IP[$existing]}" != "$candidate" ] || duplicate=1; done
    if [ "$duplicate" -eq 1 ]; then printf '[WARN] IP %s is already registered.\n' "$candidate"; continue; fi
    if ! n="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$candidate" hostname -s 2>/dev/null)"; then
      if [ "$bundled" -eq 1 ]; then
        if ! n="$(enroll_node "$candidate" "$enrollment_code")"; then
          unset enrollment_code
          printf '[WARN] Pairing failed for %s. Check its current code and try again.\n' "$candidate" >&2
          continue
        fi
      else
        while true; do
          read -r -p "Pairing code for $candidate (visible): " enrollment_code
          if n="$(enroll_node "$candidate" "$enrollment_code")"; then break; fi
          printf '[WARN] Pairing failed for %s. Check its current code and try again.\n' "$candidate" >&2
        done
      fi
      unset enrollment_code
      [ "$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$candidate" hostname -s)" = "$n" ] || die "$candidate SSH identity does not match the enrolled node"
    fi
    unset enrollment_code
    [[ "$n" =~ ^(lb|cp|w)[1-9][0-9]*$ ]] || die "$candidate has an invalid hostname: $n"
    [ -z "${IP[$n]+set}" ] || die "$n appears more than once in the inventory"
    IP[$n]="$candidate"
    NODES+=("$n")
    printf '  %-8s %s\n' "$n" "$candidate"
  done
fi
rebuild_groups
validate_inventory
declare -A USED_IP
for n in "${NODES[@]}"; do
  valid_ip "${IP[$n]:-}" || die "$n has an invalid IP"
  [ -z "${USED_IP[${IP[$n]}]:-}" ] || die "$n and ${USED_IP[${IP[$n]}]} have the same IP"
  USED_IP[${IP[$n]}]="$n"
done
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] && [ "$SSH_USER" != root ] || die 'Invalid SSH username'
getent passwd "$SSH_USER" >/dev/null || die 'SSH user does not exist locally'
command -v ssh >/dev/null && command -v scp >/dev/null || die 'OpenSSH client is missing'
setup_ssh
LOCAL_IP="$(awk -F= '$1=="IP" {print $2; exit}' "$STATE/node.conf")"
[ "${IP[$SELF]}" = "$LOCAL_IP" ] || die "Local node $SELF does not use its phase-1 IP $LOCAL_IP"
remote() { ssh "${SSH_OPTS[@]}" "$SSH_USER@${IP[$1]}" "$2"; }
run() { local n="$1" cmd="$2"; if [ "$n" = "$SELF" ]; then bash -lc "$cmd"; else remote "$n" "sudo -n bash -lc $(q "$cmd")"; fi; }
pin_host_key() {
  local host="$1" target="$2" public_key="$3" user_file cmd
  user_file="${NODE_HOME[$host]}/.ssh/known_hosts"
  cmd="set -e; install -d -m 700 /root/.ssh; touch /root/.ssh/known_hosts; touch $(q "$user_file"); for file in /root/.ssh/known_hosts $(q "$user_file"); do ssh-keygen -R $(q "${IP[$target]}") -f \"\$file\" >/dev/null 2>&1 || true; ssh-keygen -R $(q "$target") -f \"\$file\" >/dev/null 2>&1 || true; printf '%s\\n' $(q "${IP[$target]} $public_key") $(q "$target $public_key") >> \"\$file\"; chmod 600 \"\$file\"; done; chown $(q "$SSH_USER") $(q "$user_file")"
  run "$host" "$cmd"
}
stage() { local n="$1" script="$2" extra="${3:-}"; shift 3 || true; local cmd arg; cmd="env VIP=$(q "$VIP") API_PORT=$(q "$API_PORT") IFACE=$(q "${NODE_IFACE[$n]}") CP1=$(q "${IP[cp1]}") POD_CIDR=$(q "$POD_CIDR") SVC_CIDR=$(q "$SVC_CIDR") VRRP_ID=$(q "$VRRP_ID") K8S_TEST_MODE=$(q "$TEST_MODE") $extra bash $LIB/$script"; for arg in "$@"; do cmd="$cmd $(q "$arg")"; done; run "$n" "$cmd"; }
blank_data_disks() {
  local cmd
  cmd="$(cat <<'SH'
set -e
root_src=$(findmnt -no SOURCE /)
root_disk=$(lsblk -sno NAME "$root_src" 2>/dev/null | tail -n 1 | sed 's/[^A-Za-z0-9._-]//g')
[ -n "$root_disk" ] || exit 0
while read -r name type; do
  [ "$type" = disk ] || continue
  [ "$name" != "$root_disk" ] || continue
  dev="/dev/$name"
  [ "$(lsblk -nr -o NAME "$dev" | wc -l)" -eq 1 ] || continue
  [ -z "$(lsblk -nr -o MOUNTPOINTS "$dev" | tr -d '[:space:]')" ] || continue
  signatures=$(wipefs -n "$dev" 2>/dev/null) || continue
  [ -z "$signatures" ] || continue
  printf '%s\n' "$dev"
done < <(lsblk -dn -o NAME,TYPE)
SH
)"
  run "$1" "$cmd"
}
wait_reboot() {
  local n="$1" i down=0
  for ((i=0;i<24;i++)); do if ! remote "$n" true >/dev/null 2>&1; then down=1; break; fi; sleep 5; done
  [ "$down" -eq 1 ] || die "$n did not disconnect within 120 seconds; reboot could not be confirmed"
  for ((i=0;i<90;i++)); do if remote "$n" true >/dev/null 2>&1; then return 0; fi; sleep 5; done
  die "$n did not restore SSH within 450 seconds after reboot"
}
say "Checking phase 1 completion and administrator SSH access on ${#NODES[@]} nodes"
for n in "${NODES[@]}"; do
  if [ "$n" != "$SELF" ]; then remote "$n" 'sudo -n test -f /var/lib/k8s-deploy/ready && sudo -n true' || die "$n public-key SSH or noninteractive sudo failed; use the node enrollment code on the first run"; fi
  [ "$(run "$n" hostname -s)" = "$n" ] || die "$n hostname does not match its registered IP"
  NODE_HOME[$n]="$(run "$n" "getent passwd $(q "$SSH_USER") | cut -d: -f6")"
  [[ "${NODE_HOME[$n]}" =~ ^/[a-zA-Z0-9._/-]+$ ]] || die "$n has an invalid SSH home directory"
  PUB[$n]="$(run "$n" "cat $(q "${NODE_HOME[$n]}/.ssh/id_ed25519.pub")")"
  [[ "${PUB[$n]}" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die "$n has an invalid node SSH public key"
done
say 'Configuring hostname resolution and mutual SSH public-key trust'
HOSTS_FILE="$(mktemp)"; KEYS_FILE="$(mktemp)"; chmod 600 "$HOSTS_FILE" "$KEYS_FILE"
trap 'rm -f "$HOSTS_FILE" "$KEYS_FILE"' EXIT
{
  echo '# BEGIN K8S CLUSTER'
  for n in "${NODES[@]}"; do printf '%s %s\n' "${IP[$n]}" "$n"; done
  echo '# END K8S CLUSTER'
} > "$HOSTS_FILE"
for n in "${NODES[@]}"; do printf '%s\n' "${PUB[$n]}" >> "$KEYS_FILE"; done
for n in "${NODES[@]}"; do
  if [ "$n" = "$SELF" ]; then
    install -m 600 "$HOSTS_FILE" "$STATE/cluster-hosts.tmp"
    while IFS= read -r key; do grep -Fqx -- "$key" "${NODE_HOME[$n]}/.ssh/authorized_keys" || printf '%s\n' "$key" >> "${NODE_HOME[$n]}/.ssh/authorized_keys"; done < "$KEYS_FILE"
  else
    remote "$n" "sudo -n tee $STATE/cluster-hosts.tmp >/dev/null" < "$HOSTS_FILE"
    remote "$n" "sudo -n tee /var/lib/k8s-deploy/mesh-keys >/dev/null" < "$KEYS_FILE"
    run "$n" "while IFS= read -r key; do grep -Fqx -- \"\$key\" $(q "${NODE_HOME[$n]}/.ssh/authorized_keys") || printf '%s\\n' \"\$key\" >> $(q "${NODE_HOME[$n]}/.ssh/authorized_keys"); done < /var/lib/k8s-deploy/mesh-keys; rm -f /var/lib/k8s-deploy/mesh-keys; chown $(q "$SSH_USER") $(q "${NODE_HOME[$n]}/.ssh/authorized_keys"); chmod 600 $(q "${NODE_HOME[$n]}/.ssh/authorized_keys")"
  fi
  run "$n" 'set -e; hosts_tmp=$(mktemp); cat /var/lib/k8s-deploy/cluster-hosts.tmp > "$hosts_tmp"; sed -e "/^# BEGIN K8S CLUSTER$/,/^# END K8S CLUSTER$/d" -e "/^# BEGIN K8S NODE$/,/^# END K8S NODE$/d" -e "/^127\.0\.1\.1[[:space:]]/d" /etc/hosts >> "$hosts_tmp"; cat "$hosts_tmp" > /etc/hosts; rm -f "$hosts_tmp" /var/lib/k8s-deploy/cluster-hosts.tmp'
done
for n in "${NODES[@]}"; do
  for peer in "${NODES[@]}"; do
    resolved="$(run "$n" "getent ahostsv4 $(q "$peer") | awk 'NR==1{print \$1}'")"
    [ "$resolved" = "${IP[$peer]}" ] || die "$n resolves $peer to $resolved; expected ${IP[$peer]}"
    [ "$n" = "$peer" ] && continue
    cmd="sudo -H -u $(q "$SSH_USER") ssh -i $(q "${NODE_HOME[$n]}/.ssh/id_ed25519") -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new $(q "$SSH_USER@$peer") true"
    run "$n" "$cmd" || die "$n cannot reach $peer by hostname using SSH keys"
  done
done
say 'Closing the one-time enrollment service after SSH mesh verification'
for n in "${NODES[@]}"; do
  run "$n" 'systemctl disable --now k8s-enroll.service >/dev/null 2>&1 || true; rm -f /var/lib/k8s-deploy/enroll-token; touch /var/lib/k8s-deploy/enroll-done'
done
say 'Pulling phase-1 node.conf from every node and registering node configuration'
for n in "${NODES[@]}"; do
  fetch_manifest "$n"
  run "$n" "ip -o -4 addr show $(q "${NODE_IFACE[$n]}") | grep -Fq $(q " ${IP[$n]}/")" || die "$n does not use its manifest IP ${IP[$n]} on ${NODE_IFACE[$n]}"
done
if [ ! -f "$CONF" ]; then
  PROFILE="$(ask 'Deployment profile (standard/test)' "$PROFILE")"
  VIP="$(ask 'API VIP' "$VIP")"
  API_PORT="$(ask 'API port' "$API_PORT")"
  VRRP_ID="$(ask 'VRRP ID' "$VRRP_ID")"
  POD_CIDR="$(ask 'Pod CIDR' "$POD_CIDR")"
  SVC_CIDR="$(ask 'Service CIDR' "$SVC_CIDR")"
  for n in "${K8S_NODES[@]}"; do
    disk_default="${NODE_DATA[$n]:-}"
    if [ -z "$disk_default" ]; then
      candidates="$(blank_data_disks "$n")" || die "$n data disk inspection failed"
      disk_default=none
      if [ -n "$candidates" ]; then
        mapfile -t candidate_disks <<< "$candidates"
        if [ "${#candidate_disks[@]}" -eq 1 ]; then
          disk_default="${candidate_disks[0]}"
        else
          printf '[WARN] %s has multiple blank disks: %s; choose the intended disk explicitly.\n' "$n" "${candidate_disks[*]}"
        fi
      fi
    fi
    DISK[$n]="$(ask "$n data disk (/dev/... or none for system disk)" "$disk_default")"
  done
fi
if [ -z "${AUTH_PASS:-}" ]; then
  [ -t 0 ] || die 'VRRP password is missing during resume; rerun from an interactive terminal'
  read -r -s -p 'Shared VRRP password for load balancers (1-8 ASCII characters): ' AUTH_PASS; printf '\n'
fi
[[ "$AUTH_PASS" =~ ^[A-Za-z0-9._-]{1,8}$ ]] || die 'Invalid VRRP password'
case "$PROFILE" in standard) TEST_MODE=0;; test) TEST_MODE=1;; *) die 'Profile must be standard or test';; esac
[[ "$API_PORT" =~ ^[0-9]+$ ]] && [ "$API_PORT" -ge 1 ] && [ "$API_PORT" -le 65535 ] || die 'Invalid API port'
[[ "$VRRP_ID" =~ ^[0-9]+$ ]] && [ "$VRRP_ID" -ge 1 ] && [ "$VRRP_ID" -le 255 ] || die 'Invalid VRRP ID'
valid_ip "$VIP" || die 'Invalid VIP'
for n in "${NODES[@]}"; do [ "${IP[$n]}" != "$VIP" ] || die "$n IP conflicts with VIP"; done
for n in "${K8S_NODES[@]}"; do
  [ "${DISK[$n]:-}" = none ] || [[ "${DISK[$n]:-}" =~ ^/dev/[a-zA-Z0-9._/-]+$ ]] || die "$n data disk must be none or an absolute /dev/... path"
done
if [ ! -f "$CONF" ]; then
  printf 'Plan: configure %d nodes and VIP %s. Blank data disks may be formatted.\n' "${#NODES[@]}" "$VIP"
  for n in "${NODES[@]}"; do printf '  %-8s %-8s %s' "$n" "$(role "$n")" "${IP[$n]}"; [[ "$n" = lb* ]] || printf '  data disk: %s' "${DISK[$n]}"; printf '\n'; done
  [ "$(ask 'Type DEPLOY ALL to authorize deployment and blank data-disk initialization' NO)" = 'DEPLOY ALL' ] || die 'Cancelled'
  install -d -m 700 "$STATE"
  {
    printf 'NODES=('; for n in "${NODES[@]}"; do printf '%q ' "$n"; done; printf ')\n'
    for v in SSH_USER PROFILE VIP API_PORT VRRP_ID POD_CIDR SVC_CIDR AUTH_PASS; do printf '%s=%q\n' "$v" "${!v}"; done
    for n in "${NODES[@]}"; do printf 'IP[%q]=%q\n' "$n" "${IP[$n]}"; done
    for n in "${NODES[@]}"; do
      printf 'NODE_IFACE[%q]=%q\nNODE_HOME[%q]=%q\nPUB[%q]=%q\n' \
        "$n" "${NODE_IFACE[$n]}" "$n" "${NODE_HOME[$n]}" "$n" "${PUB[$n]}"
    done
    for n in "${K8S_NODES[@]}"; do printf 'DISK[%q]=%q\n' "$n" "${DISK[$n]}"; done
  } > "$CONF"
  chmod 600 "$CONF"
elif ! grep -q '^AUTH_PASS=' "$CONF"; then
  printf 'AUTH_PASS=%q\n' "$AUTH_PASS" >> "$CONF"
fi
say 'Checking uniqueness of machine-id, product_uuid, MAC and SSH host keys'
declare -A SEEN_ID SEEN_UUID SEEN_MAC SEEN_HOSTKEY
for n in "${NODES[@]}"; do
  ID[$n]="$(run "$n" 'cat /etc/machine-id')"
  UUID[$n]="$(run "$n" 'cat /sys/class/dmi/id/product_uuid')"
  MAC[$n]="$(run "$n" "cat /sys/class/net/$(q "${NODE_IFACE[$n]}")/address")"
  HOSTKEY[$n]="$(run "$n" 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '\''{print $2}'\''')"
  [ -n "${ID[$n]}" ] && [ -n "${UUID[$n]}" ] && [ -n "${MAC[$n]}" ] && [ -n "${HOSTKEY[$n]}" ] || die "$n has a missing machine identity"
  [ -z "${SEEN_UUID[${UUID[$n]}]:-}" ] || die "$n and ${SEEN_UUID[${UUID[$n]}]} share product_uuid; fix this in the hypervisor"
  [ -z "${SEEN_MAC[${MAC[$n]}]:-}" ] || die "$n and ${SEEN_MAC[${MAC[$n]}]} share a MAC address; fix this in the hypervisor"
  if [ -n "${SEEN_HOSTKEY[${HOSTKEY[$n]}]:-}" ]; then
    run "$n" 'test ! -e /etc/kubernetes/kubelet.conf && test ! -e /etc/kubernetes/admin.conf' || die "$n already has kubeadm state; refusing to regenerate SSH host keys"
    say "$n has a duplicate SSH host key; generating a unique key and distributing its verified public key"
    new_host_key="$(run "$n" 'set -e; backup=/var/lib/k8s-deploy/ssh-host-keys-before-repair; install -d -m 700 "$backup"; for f in /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; do if [ -e "$f" ]; then cp -a "$f" "$backup/"; fi; done; rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; if ! ssh-keygen -A >/dev/null || ! sshd -t; then rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; for f in "$backup"/*; do if [ -e "$f" ]; then cp -a "$f" /etc/ssh/; fi; done; exit 1; fi; cat /etc/ssh/ssh_host_ed25519_key.pub')" || die "$n could not generate a valid SSH host key"
    [[ "$new_host_key" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die "$n returned an invalid SSH host public key"
    run "$n" 'systemctl restart ssh' || true
    pin_host_key "$SELF" "$n" "$new_host_key"
    for ((i=0;i<24;i++)); do if remote "$n" true >/dev/null 2>&1; then break; fi; sleep 5; done
    remote "$n" true >/dev/null || die "$n is unreachable by SSH with its new verified host key"
    for host in "${NODES[@]}"; do
      [ "$host" = "$SELF" ] && continue
      pin_host_key "$host" "$n" "$new_host_key"
    done
    run "$n" 'rm -f /var/lib/k8s-deploy/ssh-host-keys-before-repair/*; rmdir /var/lib/k8s-deploy/ssh-host-keys-before-repair; touch /var/lib/k8s-deploy/ssh-host-keys-unique'
    exec bash "$0" --resume
  fi
  SEEN_UUID[${UUID[$n]}]="$n"; SEEN_MAC[${MAC[$n]}]="$n"; SEEN_HOSTKEY[${HOSTKEY[$n]}]="$n"
  if [ -n "${SEEN_ID[${ID[$n]}]:-}" ]; then
    run "$n" 'test ! -e /etc/kubernetes/kubelet.conf && test ! -e /etc/kubernetes/admin.conf' || die "$n already has kubeadm state; refusing to change machine-id"
    say "$n and ${SEEN_ID[${ID[$n]}]} share machine-id; generating a new ID and rebooting"
    if [ "$n" = "$SELF" ]; then
      install -d -m 755 "$LIB"
      for f in "${STAGES[@]}"; do if [ "$STAGE_DIR" != "$LIB" ]; then install -m 755 "$STAGE_DIR/$f" "$LIB/$f"; fi; done
      [ "$(readlink -f "$0")" = "$LIB/02-deploy-cluster.sh" ] || install -m 755 "$0" "$LIB/02-deploy-cluster.sh"
      cat > /etc/systemd/system/k8s-cluster-resume.service <<EOF
[Unit]
Description=Resume Kubernetes cluster deployment after reboot
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/bash $LIB/02-deploy-cluster.sh --resume
[Install]
WantedBy=multi-user.target
EOF
      systemctl daemon-reload; systemctl enable k8s-cluster-resume.service >/dev/null
    fi
    run "$n" 'new_id=$(tr -d - < /proc/sys/kernel/random/uuid); [[ "$new_id" =~ ^[a-f0-9]{32}$ ]] || exit 1; printf "%s\n" "$new_id" > /etc/machine-id; if [ -e /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then printf "%s\n" "$new_id" > /var/lib/dbus/machine-id; fi; systemctl reboot' || true
    [ "$n" = "$SELF" ] && exit 0
    wait_reboot "$n"
    exec bash "$0" --resume
  fi
  SEEN_ID[${ID[$n]}]="$n"
done
say 'Distributing deployment scripts and installing Kubernetes prerequisites'
install -d -m 755 "$LIB"
for f in "${STAGES[@]}"; do [ -f "$STAGE_DIR/$f" ] || die "Missing $f"; if [ "$STAGE_DIR" != "$LIB" ]; then install -m 755 "$STAGE_DIR/$f" "$LIB/$f"; fi; done
for n in "${NODES[@]}"; do
  [ "$n" = "$SELF" ] && continue
  remote "$n" 'install -d -m 700 ~/.k8s-deploy'
  for f in "${STAGES[@]}"; do scp "${SSH_OPTS[@]}" "$LIB/$f" "$SSH_USER@${IP[$n]}:.k8s-deploy/$f" >/dev/null; done
  run "$n" "install -d -m 755 $LIB; for f in ${STAGES[*]}; do install -m 755 $(q "${NODE_HOME[$n]}/.k8s-deploy")/\$f $LIB/\$f; done"
done
for n in "${NODES[@]}"; do
  if [[ "$n" = lb* ]]; then continue; fi
  stage "$n" setup-k8s-node.sh "DATA_DEVICE=$(q "${DISK[$n]}")" "${IP[$n]}" "$n" "$(role "$n")" --k8s-only --yes
done
say "Configuring ${#LBS[@]} load balancer(s)"
CP_BACKENDS=""
for cp in "${CPS[@]}"; do CP_BACKENDS="${CP_BACKENDS:+$CP_BACKENDS }$cp=${IP[$cp]}"; done
for i in "${!LBS[@]}"; do
  n="${LBS[$i]}"
  mode=--slave; [ "$n" = lb1 ] && mode=--master
  priority=$((250 - i))
  cmd="env VIP=$(q "$VIP") API_PORT=$(q "$API_PORT") IFACE=$(q "${NODE_IFACE[$n]}") CP_BACKENDS=$(q "$CP_BACKENDS") VRRP_ID=$(q "$VRRP_ID") LB_PRIORITY=$(q "$priority") AUTH_PASS=$(q "$AUTH_PASS") bash $LIB/setup-lb.sh $(q "${IP[$n]}") $mode"
  run "$n" "$cmd"
done
say 'Checking VIP ARP ownership from every Kubernetes node before using the API endpoint'
for node in "${K8S_NODES[@]}"; do
  for ((probe=1; probe<=3; probe++)); do
    vip_mac="$(run "$node" "ip neigh del $(q "$VIP") dev $(q "${NODE_IFACE[$node]}") >/dev/null 2>&1 || true; ping -c 1 -W 2 $(q "$VIP") >/dev/null 2>&1 || true; ip neigh show $(q "$VIP") dev $(q "${NODE_IFACE[$node]}") | awk '{for(i=1;i<=NF;i++) if(\$i==\"lladdr\") {print \$(i+1); exit}}'")"
    [ -n "$vip_mac" ] || die "$node could not resolve VIP $VIP by ARP; check VRRP and the network"
    vip_owner=''
    for n in "${LBS[@]}"; do [ "$vip_mac" != "${MAC[$n]}" ] || vip_owner="$n"; done
    [ -n "$vip_owner" ] || die "VIP $VIP resolves to unexpected MAC $vip_mac on $node; choose an unused VIP before continuing"
    sleep 2
  done
done
say "VIP $VIP resolves to a registered load balancer from every Kubernetes node"
for n in "${K8S_NODES[@]}"; do stage "$n" configure-registry-mirror.sh ''; done
if ! run cp1 'test -s /etc/kubernetes/admin.conf'; then
  run cp1 'kubeadm config images pull --kubernetes-version v1.36.5 --cri-socket unix:///run/containerd/containerd.sock'
  stage cp1 init-cluster.sh "DATA_DEVICE=$(q "${DISK[cp1]}")"
fi
say 'Waiting for cp1 API to become ready through the VIP'
api_ready=0
for ((attempt=1; attempt<=60; attempt++)); do
  if run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl --request-timeout=5s get --raw=/readyz' >/dev/null 2>&1; then
    api_ready=1
    break
  fi
  sleep 3
done
[ "$api_ready" -eq 1 ] || die 'cp1 API did not become ready through the VIP within 3 minutes; inspect kube-apiserver and HAProxy backends'
stage cp1 install-cilium.sh 'CILIUM_OPERATOR_REPLICAS=1'
run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl wait --for=condition=Ready node/cp1 --timeout=10m'
need_join=0
for n in "${JOIN_NODES[@]}"; do run cp1 "KUBECONFIG=/etc/kubernetes/admin.conf kubectl get node $(q "$n") >/dev/null 2>&1" || need_join=1; done
[ "$need_join" -eq 0 ] || stage cp1 create-join-credentials.sh '' --quiet
for n in "${JOIN_NODES[@]}"; do
  if ! run cp1 "KUBECONFIG=/etc/kubernetes/admin.conf kubectl get node $(q "$n") >/dev/null 2>&1"; then
    run "$n" 'test ! -e /etc/kubernetes/kubelet.conf && test ! -e /etc/kubernetes/bootstrap-kubelet.conf' || die "$n has partial kubeadm state; automatic join is stopped"
    file=worker-join.txt; [[ "$n" = cp* ]] && file=cp-join.txt
    cred="$(run cp1 "cat /root/k8s-join/$file")"
    run "$n" 'install -d -m 700 /root/k8s-join'
    if [ "$n" = "$SELF" ]; then printf '%s\n' "$cred" > "/root/k8s-join/$file"; chmod 600 "/root/k8s-join/$file"; else printf '%s\n' "$cred" | remote "$n" "sudo -n tee /root/k8s-join/$file >/dev/null"; run "$n" "chmod 600 /root/k8s-join/$file"; fi
    unset cred
    stage "$n" join-nodes.sh "CRED_DIR=/root/k8s-join DATA_DEVICE=$(q "${DISK[$n]}")" "${IP[$n]}" "$(role "$n")"
  fi
  run cp1 "KUBECONFIG=/etc/kubernetes/admin.conf kubectl wait --for=condition=Ready node/$n --timeout=10m"
  if [[ "$n" = cp* ]]; then stage cp1 configure-coredns-ha.sh ''; fi
done
operator_replicas=1; [ "${#CPS[@]}" -ge 2 ] && operator_replicas=2
stage cp1 install-cilium.sh "CILIUM_OPERATOR_REPLICAS=$operator_replicas"
stage cp1 configure-coredns-ha.sh ''
say 'Running final cluster checks'
run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes -o wide'
for n in "${K8S_NODES[@]}"; do run cp1 "KUBECONFIG=/etc/kubernetes/admin.conf kubectl wait --for=condition=Ready node/$n --timeout=2m"; done
run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status deployment/coredns --timeout=5m'
run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status deployment/cilium-operator --timeout=5m'
run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status daemonset/cilium --timeout=5m'
etcd_rows="$(run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system get pods -l component=etcd --no-headers')"
printf '%s\n' "$etcd_rows" | awk -v expected="${#CPS[@]}" '$2=="1/1" && $3=="Running"{ok++} END{exit !(NR==expected && ok==expected)}' || die "All ${#CPS[@]} etcd Pods must be Running"
dns_rows="$(run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide --no-headers')"
printf '%s\n' "$dns_rows" | awk -v expected="${#CPS[@]}" -v names=" ${CPS[*]} " '$2=="1/1" && $3=="Running" && index(names," "$7" "){ok++; nodes[$7]=1} END{for(n in nodes) distinct++; exit !(NR==expected && ok==expected && distinct==expected)}' || die "CoreDNS Pods must run separately on all ${#CPS[@]} control-plane nodes"
for n in "${LBS[@]}"; do run "$n" 'systemctl is-active --quiet haproxy && systemctl is-active --quiet keepalived'; done
curl -kfsS --connect-timeout 5 "https://$VIP:$API_PORT/readyz" | grep -qx ok || die 'VIP API /readyz check failed'
run cp1 'if [ -f /root/k8s-join/worker-join.txt ]; then token=$(awk '\''{for(i=1;i<=NF;i++) if($i=="--token") {print $(i+1); exit}}'\'' /root/k8s-join/worker-join.txt); case "$token" in *.*) kubeadm token delete "${token%%.*}" >/dev/null 2>&1 || true;; esac; fi; rm -f /root/k8s-join/*-join.txt'
for n in "${JOIN_NODES[@]}"; do run "$n" 'rm -f /root/k8s-join/*-join.txt'; done
touch "$STATE/cluster-ready"
sed -i '/^AUTH_PASS=/d' "$CONF"
systemctl disable k8s-cluster-resume.service >/dev/null 2>&1 || true
say 'Cluster deployment and final checks complete'
