#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Atlas KubeKit | By Atlas Richie
# Phase 1: run on the console of every fresh Ubuntu node.
set -euo pipefail
STATE_DIR=/var/lib/k8s-deploy
LIB_DIR=/usr/local/lib/k8s-deploy
CONF=$STATE_DIR/node.conf
SERVICE=/etc/systemd/system/k8s-prepare-resume.service
ENROLL_SERVICE=/etc/systemd/system/k8s-enroll.service
ENROLL_TOKEN=$STATE_DIR/enroll-token
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
ask() { local a; read -r -p "$1 [$2]: " a; printf '%s' "${a:-$2}"; }
print_pairing_code() {
  local secret
  secret="$(cat "$ENROLL_TOKEN")"
  if [[ ! "$secret" =~ ^[0-9a-fA-F]{12}$ ]]; then
    printf '%s\n' "$secret"
    return 0
  fi
  python3 - "$IP" "$ENROLL_TOKEN" <<'PY'
import ipaddress
import sys
from pathlib import Path

alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
ip_bytes = ipaddress.IPv4Address(sys.argv[1]).packed
secret = bytes.fromhex(Path(sys.argv[2]).read_text().strip())
value = int.from_bytes(ip_bytes + secret, 'big')
code = ''.join(alphabet[(value >> shift) & 31] for shift in range(75, -1, -5))
print('-'.join(code[i:i + 4] for i in range(0, 16, 4)))
PY
}
start_enrollment() {
  local rotate="${1:-}" token_tmp
  [ -f "$STATE_DIR/ready" ] || die 'Complete phase 1 before enabling enrollment'
  command -v python3 >/dev/null || die 'Python 3 is required for private-network enrollment'
  install -d -m 755 "$LIB_DIR"
  [ "$(readlink -f "$0")" = "$LIB_DIR/01-prepare-node.sh" ] || install -m 755 "$0" "$LIB_DIR/01-prepare-node.sh"
  if [ -f "$STATE_DIR/enroll-done" ]; then
    [ "$rotate" = --rotate ] || { say 'This node is already enrolled'; return 0; }
  fi
  if [ "$rotate" = --rotate ] || [ ! -s "$ENROLL_TOKEN" ]; then
    umask 077
    token_tmp="$(mktemp "$STATE_DIR/.enroll-token.XXXXXX")"
    head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$token_tmp"
    chmod 600 "$token_tmp"
    mv -f "$token_tmp" "$ENROLL_TOKEN"
  fi
  [ "$rotate" != --rotate ] || rm -f "$STATE_DIR/enroll-done"
  chmod 600 "$ENROLL_TOKEN"
  cat > "$ENROLL_SERVICE" <<EOF
[Unit]
Description=One-time Kubernetes node enrollment over the private network
After=network-online.target ssh.service
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/bash $LIB_DIR/01-prepare-node.sh --enroll-server
Restart=no
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable k8s-enroll.service >/dev/null
  if [ "$rotate" = --rotate ]; then
    systemctl restart k8s-enroll.service
  else
    systemctl start k8s-enroll.service
  fi
  systemctl is-active --quiet k8s-enroll.service || die 'Enrollment service did not start'
  say "Private-network enrollment ready on $IP:25422"
  printf 'Pairing code (enter as one value on the phase-2 initiating node): %s\n' "$(print_pairing_code)"
}
serve_enrollment() {
  [ -f "$CONF" ] && [ -s "$ENROLL_TOKEN" ] && [ ! -e "$STATE_DIR/enroll-done" ] || die 'Enrollment is not enabled'
  . "$CONF"
  export K8S_ENROLL_IP="$IP" K8S_ENROLL_USER="$SSH_USER"
  exec python3 - <<'PY'
import hmac
import hashlib
import http.server
import json
import os
import pwd
import re
import threading
from pathlib import Path

state = Path('/var/lib/k8s-deploy')
secret = bytes.fromhex((state / 'enroll-token').read_text().strip())
user = os.environ['K8S_ENROLL_USER']
home = Path(pwd.getpwnam(user).pw_dir)
host_key = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()
name = Path('/etc/hostname').read_text().strip().split('.')[0]

class InvalidPairingCode(Exception):
    pass

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_error(400)
            return
        if self.path != '/enroll' or not 0 < length <= 4096:
            self.send_error(404)
            return
        try:
            request = json.loads(self.rfile.read(length))
            nonce, key, mac = request['nonce'], request['key'], request['mac']
            if not re.fullmatch(r'[0-9a-f]{32}', nonce):
                raise ValueError('nonce')
            if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', key):
                raise ValueError('key')
            expected = hmac.new(secret, (nonce + '\n' + key).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, mac):
                raise InvalidPairingCode()
            auth = home / '.ssh' / 'authorized_keys'
            if key not in auth.read_text().splitlines():
                with auth.open('a') as output:
                    output.write(key + '\n')
            os.chown(auth, pwd.getpwnam(user).pw_uid, pwd.getpwnam(user).pw_gid)
            os.chmod(auth, 0o600)
            reply_mac = hmac.new(secret, ('OK\n' + nonce + '\n' + name + '\n' + host_key).encode(), hashlib.sha256).hexdigest()
            body = json.dumps({'name': name, 'host_key': host_key, 'nonce': nonce, 'mac': reply_mac}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            (state / 'enroll-done').touch()
            (state / 'enroll-token').unlink(missing_ok=True)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except InvalidPairingCode:
            self.send_error(401, 'Invalid pairing code')
        except (KeyError, ValueError):
            self.send_error(400, 'Invalid enrollment request')
        except OSError:
            self.send_error(500, 'Enrollment service error')

http.server.HTTPServer((os.environ['K8S_ENROLL_IP'], 25422), Handler).serve_forever()
PY
}
valid_ip() { local x p; [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1; IFS=. read -r -a x <<< "$1"; for p in "${x[@]}"; do [ "$p" -le 255 ] || return 1; done; }
save() { install -d -m 700 "$STATE_DIR"; { for v in NAME NETWORK_MODE IP IFACE PREFIX GATEWAY SSH_USER ADMIN_KEY DATA_DEVICE; do printf '%s=%q\n' "$v" "${!v:-}"; done; } > "$CONF"; chmod 600 "$CONF"; }
publish_manifest() {
  local home_dir group manifest_dir tmp public_key node_role
  [ "$(hostname -s)" = "$NAME" ] || die 'Current hostname does not match the phase-1 configuration'
  ip -o -4 addr show "$IFACE" | grep -Fq " $IP/$PREFIX " || die 'Current IPv4 address does not match the phase-1 configuration'
  home_dir="$(getent passwd "$SSH_USER" | cut -d: -f6)"
  group="$(id -gn "$SSH_USER")"
  [ -s "$home_dir/.ssh/id_ed25519.pub" ] || die 'Node SSH public key is missing'
  public_key="$(cat "$home_dir/.ssh/id_ed25519.pub")"
  [[ "$public_key" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die 'Node SSH public key is invalid'
  case "$NAME" in lb*) node_role=lb;; cp*) node_role=cp;; w*) node_role=worker;; esac
  manifest_dir="$home_dir/.k8s-deploy"
  install -d -o "$SSH_USER" -g "$group" -m 700 "$manifest_dir"
  tmp="$(mktemp "$manifest_dir/.node.conf.XXXXXX")"
  {
    printf 'VERSION=1\nPHASE1_READY=1\nNAME=%s\nROLE=%s\nIP=%s\nPREFIX=%s\nIFACE=%s\nNETWORK_MODE=%s\nSSH_USER=%s\nSSH_HOME=%s\nNODE_PUBLIC_KEY=%s\nDATA_DEVICE=%s\nOS_ID=ubuntu\nOS_VERSION=24.04\nARCH=amd64\n' \
      "$NAME" "$node_role" "$IP" "$PREFIX" "$IFACE" "$NETWORK_MODE" "$SSH_USER" "$home_dir" "$public_key" "${DATA_DEVICE:-}"
  } > "$tmp"
  chown "$SSH_USER:$group" "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$manifest_dir/node.conf"
}
case "${1:-}" in
  --help|-h) echo 'Atlas KubeKit | By Atlas Richie'; echo 'Usage: sudo bash 01-prepare-node.sh [--config-only|--status|--code|--pair]'; exit 0 ;;
  --status) if [ -e "$STATE_DIR/ready" ]; then echo 'Phase 1 complete'; elif systemctl is-failed --quiet k8s-prepare-resume.service 2>/dev/null; then echo 'Phase 1 resume failed; inspect: journalctl -u k8s-prepare-resume.service -b'; elif [ -e "$STATE_DIR/reboot-pending" ]; then echo 'Waiting for reboot and automatic resume'; elif [ -f "$CONF" ]; then echo 'Configuration saved; Phase 1 not applied'; else echo 'Phase 1 incomplete'; fi; exit 0 ;;
  --enroll-server) [ "$(id -u)" -eq 0 ] || die 'Run as root'; serve_enrollment ;;
  --code|--enroll-code) [ "$(id -u)" -eq 0 ] || die 'Run as root'; [ -s "$ENROLL_TOKEN" ] || die 'No pending enrollment code'; [ -f "$CONF" ] || die 'Saved phase-1 configuration is missing'; [ "$(stat -c %u "$CONF")" = 0 ] && [ "$(stat -c %a "$CONF")" = 600 ] || die 'node.conf owner or permissions are unsafe'; . "$CONF"; print_pairing_code; exit 0 ;;
  ''|--config-only|--resume|--publish|--enable-enrollment|--pair|-p|--generate-enrollment-code|--renew-enrollment) ;;
  *) die 'Unknown argument' ;;
esac
if [ -t 0 ]; then say 'Atlas KubeKit | By Atlas Richie | Phase 1: node preparation'; fi
[ "$(id -u)" -eq 0 ] || die 'Run this script with sudo'
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || die 'Only Ubuntu Server 24.04 LTS is supported'
[ "$(dpkg --print-architecture)" = amd64 ] || die 'Only amd64 is supported'
if [ "${1:-}" != --config-only ] && [ "${1:-}" != --publish ]; then
  [ "$(free -m | awk '/^Mem:/{print $2}')" -ge 1800 ] || die 'At least 2 GiB of RAM is required'
  [ "$(df -Pk / | awk 'NR==2{print $4}')" -ge 10485760 ] || die 'At least 10 GiB of free root filesystem space is required'
fi
if [ -f "$CONF" ]; then
  [ "$(stat -c %u "$CONF")" = 0 ] && [ "$(stat -c %a "$CONF")" = 600 ] || die 'node.conf owner or permissions are unsafe'
  # This file is generated by save() and restricted to root.
  . "$CONF"
else
  case "${1:-}" in --config-only|--publish|--resume) die 'Saved phase-1 configuration is missing; run the normal phase-1 flow first';; esac
  [ "${1:-}" != --resume ] && [ -t 0 ] || die 'The first run requires an interactive terminal'
  NAME="$(ask 'Node hostname (lbN/cpN/wN)' "$(hostname -s)")"
  [[ "$NAME" =~ ^(lb|cp|w)[1-9][0-9]*$ ]] || die 'Hostname must match lbN, cpN, or wN (N >= 1)'
  DEFAULT_IP="$(ip -o -4 addr show scope global | awk 'NR==1{split($4,a,"/");print a[1]}')"
  case "$NAME" in
    lb1) DEFAULT_IP=10.20.1.20 ;; lb2) DEFAULT_IP=10.20.1.21 ;;
    cp1) DEFAULT_IP=10.20.1.22 ;; cp2) DEFAULT_IP=10.20.1.23 ;; cp3) DEFAULT_IP=10.20.1.24 ;;
    w1) DEFAULT_IP=10.20.1.25 ;; w2) DEFAULT_IP=10.20.1.26 ;; w3) DEFAULT_IP=10.20.1.27 ;;
  esac
  IFACE="$(ask 'Network interface' "$(ip -o -4 route show default | awk 'NR==1{print $5}')")"
  [[ "$IFACE" =~ ^[a-zA-Z0-9_.:-]+$ ]] && [ -d "/sys/class/net/$IFACE" ] || die 'Network interface does not exist'
  CURRENT_CIDR="$(ip -o -4 addr show dev "$IFACE" scope global | awk 'NR==1{print $4}')"
  [ -n "$CURRENT_CIDR" ] || die "No global IPv4 address on $IFACE"
  CURRENT_IP="${CURRENT_CIDR%/*}"
  CURRENT_PREFIX="${CURRENT_CIDR#*/}"
  NETWORK_MODE="$(ask 'Network mode (keep existing IP or configure static IP: keep/static)' keep)"
  case "$NETWORK_MODE" in
    keep)
      IP="$(ask 'Existing IPv4 address to register (no network changes)' "$CURRENT_IP")"
      REGISTERED_CIDR="$(ip -o -4 addr show dev "$IFACE" scope global | awk -v ip="$IP" 'index($4, ip "/") == 1 {print $4; exit}')"
      [ -n "$REGISTERED_CIDR" ] || die "IP $IP is not configured on $IFACE"
      PREFIX="${REGISTERED_CIDR#*/}"
      GATEWAY=""
      printf 'Keeping provider network settings: %s on %s. Gateway and DNS will not be changed.\n' "$REGISTERED_CIDR" "$IFACE"
      ;;
    static)
      IP="$(ask 'Final static IPv4 address for this node' "$DEFAULT_IP")"
      PREFIX="$(ask 'IPv4 prefix length' "$CURRENT_PREFIX")"
      GATEWAY="$(ask 'Default gateway' "$(ip -o -4 route show default | awk 'NR==1{print $3}')")"
      ;;
    *) die 'Network mode must be keep or static' ;;
  esac
  SSH_USER="$(ask 'SSH administrator user' ubuntu)"
  DATA_DEVICE=''
  if [[ "$NAME" != lb* ]]; then
    DATA_DEVICE="$(ask 'Dedicated Kubernetes data disk (optional; blank to choose in phase 2)' '')"
    [ -z "$DATA_DEVICE" ] || [[ "$DATA_DEVICE" =~ ^/dev/[a-zA-Z0-9._/-]+$ ]] || die 'Invalid data-disk path'
  fi
  KEY_SOURCE="$(ask 'Administrator public key source (auto, absolute .pub file path, paste, none)' auto)"
  case "$KEY_SOURCE" in
    auto)
      PUB_HOME="$(getent passwd "$SSH_USER" | cut -d: -f6)"
      AUTH_EXISTING="$PUB_HOME/.ssh/authorized_keys"
      [ -f "$AUTH_EXISTING" ] || die "No existing authorized_keys at $AUTH_EXISTING; provide an absolute .pub file path"
      mapfile -t EXISTING_KEYS < <(awk '$1 == "ssh-ed25519" && $2 ~ /^[A-Za-z0-9+/=]+$/ {print}' "$AUTH_EXISTING")
      [ "${#EXISTING_KEYS[@]}" -gt 0 ] || die "No Ed25519 key in $AUTH_EXISTING; provide an absolute .pub file path"
      if [ "${#EXISTING_KEYS[@]}" -eq 1 ]; then
        ADMIN_KEY="${EXISTING_KEYS[0]}"
      else
        printf 'Existing Ed25519 keys:\n'
        for ((i=0; i<${#EXISTING_KEYS[@]}; i++)); do
          fingerprint="$(printf '%s\n' "${EXISTING_KEYS[$i]}" | ssh-keygen -lf - | awk '{print $2}')"
          printf '  %d) %s\n' "$((i+1))" "$fingerprint"
        done
        KEY_INDEX="$(ask 'Select administrator key number' 1)"
        [[ "$KEY_INDEX" =~ ^[1-9][0-9]*$ ]] && [ "$KEY_INDEX" -le "${#EXISTING_KEYS[@]}" ] || die 'Invalid key number'
        ADMIN_KEY="${EXISTING_KEYS[$((KEY_INDEX-1))]}"
      fi
      printf 'Using existing administrator public key.\n'
      ;;
    paste) read -r -p 'Paste the administrator SSH public key (ssh-ed25519 ...): ' ADMIN_KEY ;;
    none) ADMIN_KEY=''; printf 'No external administrator key selected; use provider console and private-network enrollment.\n' ;;
    /*) [ -f "$KEY_SOURCE" ] || die "Public key file does not exist: $KEY_SOURCE"; ADMIN_KEY="$(cat "$KEY_SOURCE")" ;;
    *) die 'Choose auto, paste, none, or an absolute .pub file path' ;;
  esac
  [ -z "$ADMIN_KEY" ] || [[ "$ADMIN_KEY" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || die 'A valid Ed25519 public key is required'
  valid_ip "$IP" || die 'Invalid IPv4 address'
  if [ "$NETWORK_MODE" = static ]; then valid_ip "$GATEWAY" || die 'Invalid IPv4 gateway'; fi
  [[ "$PREFIX" =~ ^[0-9]+$ ]] && [ "$PREFIX" -ge 1 ] && [ "$PREFIX" -le 32 ] || die 'Invalid IPv4 prefix length'
  [[ "$IFACE" =~ ^[a-zA-Z0-9_.:-]+$ ]] && [ -d "/sys/class/net/$IFACE" ] || die 'Network interface does not exist'
  [[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] && [ "$SSH_USER" != root ] && getent passwd "$SSH_USER" >/dev/null || die 'SSH user does not exist'
  printf 'Plan: hostname=%s, network=%s, IP=%s/%s, SSH user=%s.\n' "$NAME" "$NETWORK_MODE" "$IP" "$PREFIX" "$SSH_USER"
  if [ "$NETWORK_MODE" = static ]; then printf 'Static gateway=%s, DNS=223.5.5.5/223.6.6.6.\n' "$GATEWAY"; fi
  case "$(ask 'Type yes to continue' NO)" in
    [Yy][Ee][Ss]) ;;
    *) die 'Cancelled' ;;
  esac
  save
fi
if [ "${1:-}" = --enable-enrollment ] || [ "${1:-}" = --pair ] || [ "${1:-}" = -p ] || [ "${1:-}" = --generate-enrollment-code ] || [ "${1:-}" = --renew-enrollment ]; then
  [ -f "$STATE_DIR/ready" ] || die 'Phase 1 is not complete'
  if [ "${1:-}" = --enable-enrollment ]; then
    start_enrollment
  else
    start_enrollment --rotate
  fi
  exit 0
fi
NETWORK_MODE="${NETWORK_MODE:-static}"
case "$NETWORK_MODE" in keep|static) ;; *) die 'Invalid saved network mode' ;; esac
if [ "${1:-}" = --config-only ] || [ "${1:-}" = --publish ]; then
  [ -f "$STATE_DIR/ready" ] || die 'Phase 1 is not complete; finish the normal flow before generating the manifest'
  publish_manifest
  say "Node manifest published at $(getent passwd "$SSH_USER" | cut -d: -f6)/.k8s-deploy/node.conf"
  exit 0
fi
if [[ "$NAME" = cp* ]]; then
  [ "$(nproc)" -ge 2 ] || die 'Control plane nodes require at least 2 vCPUs; adjust the VM and rerun'
else
  [ "$(nproc)" -ge 1 ] || die 'At least 1 vCPU is required'
fi
if [ -f "$STATE_DIR/ready" ]; then say 'Phase 1 is complete; checking configuration'; fi
install -d -m 755 "$LIB_DIR"
if [ "$(readlink -f "$0")" != "$LIB_DIR/01-prepare-node.sh" ]; then install -m 755 "$0" "$LIB_DIR/01-prepare-node.sh"; fi
cat > "$SERVICE" <<EOF
[Unit]
Description=Resume Kubernetes node preparation after reboot
After=network-online.target ssh.service
Wants=network-online.target ssh.service
[Service]
Type=oneshot
ExecStart=/usr/bin/bash $LIB_DIR/01-prepare-node.sh --resume
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable k8s-prepare-resume.service >/dev/null
if [ ! -e "$STATE_DIR/upgraded" ]; then
  say 'Updating Ubuntu packages; the system will reboot and resume automatically'
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
  DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server openssh-client sudo netplan.io curl ca-certificates gnupg python3
  touch "$STATE_DIR/upgraded" "$STATE_DIR/reboot-pending"
  systemctl reboot
  exit 0
fi
say "Configuring hostname and network (mode: $NETWORK_MODE)"
if [ -d /etc/cloud/cloud.cfg.d ]; then
  if [ "$NETWORK_MODE" = static ]; then
    printf 'preserve_hostname: true\nmanage_etc_hosts: false\nnetwork: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-k8s-static-identity.cfg
  else
    printf 'preserve_hostname: true\nmanage_etc_hosts: false\n' > /etc/cloud/cloud.cfg.d/99-k8s-static-identity.cfg
  fi
fi
[ "$(hostname -s)" = "$NAME" ] || hostnamectl set-hostname "$NAME"
if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
  sed -i -E "s/^127\\.0\\.1\\.1[[:space:]].*$/127.0.1.1 $NAME/" /etc/hosts
else
  printf '127.0.1.1 %s\n' "$NAME" >> /etc/hosts
fi
if [ "$NETWORK_MODE" = static ]; then
  NET=/etc/netplan/01-k8s.yaml
  install -d -m 755 /etc/netplan
  BACKUP=$STATE_DIR/netplan-original
  if [ ! -d "$BACKUP" ]; then
    install -d -m 700 "$BACKUP"
    for f in /etc/netplan/*.yaml; do if [ -e "$f" ]; then cp -a "$f" "$BACKUP/"; fi; done
  fi
  cat > "$NET" <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      addresses: [$IP/$PREFIX]
      routes: [{to: default, via: $GATEWAY}]
      nameservers:
        addresses: [223.5.5.5, 223.6.6.6]
EOF
  chmod 600 "$NET"
  for f in /etc/netplan/*.yaml; do if [ "$f" != "$NET" ]; then mv "$f" "$f.k8s-disabled.$(date +%s)"; fi; done
  if ! netplan generate; then
    rm -f "$NET"
    for f in "$BACKUP"/*.yaml; do if [ -e "$f" ]; then cp -a "$f" /etc/netplan/; fi; done
    die 'Netplan validation failed; original configuration restored without applying changes'
  fi
  netplan apply
  for attempt in {1..30}; do
    if ip -o -4 addr show "$IFACE" | grep -Fq " $IP/$PREFIX "; then break; fi
    sleep 1
  done
  ip -o -4 addr show "$IFACE" | grep -Fq " $IP/$PREFIX " || die 'Static IP was not applied within 30 seconds'
  for attempt in {1..30}; do
    if ip route | grep -Fq "default via $GATEWAY "; then break; fi
    sleep 1
  done
  ip route | grep -Fq "default via $GATEWAY " || die 'Default route was not applied within 30 seconds'
else
  ip -o -4 addr show "$IFACE" | grep -Fq " $IP/$PREFIX " || die "Provider IP $IP/$PREFIX is no longer present on $IFACE"
fi
getent hosts pkgs.k8s.io >/dev/null || die 'DNS lookup failed'
say 'Configuring and validating SSH public-key access'
HOME_DIR="$(getent passwd "$SSH_USER" | cut -d: -f6)"
GROUP="$(id -gn "$SSH_USER")"
install -d -o "$SSH_USER" -g "$GROUP" -m 700 "$HOME_DIR/.ssh"
AUTH="$HOME_DIR/.ssh/authorized_keys"
touch "$AUTH"
if [ -n "$ADMIN_KEY" ] && ! grep -Fqx -- "$ADMIN_KEY" "$AUTH"; then printf '%s\n' "$ADMIN_KEY" >> "$AUTH"; fi
MESH="$HOME_DIR/.ssh/id_ed25519"
if [ ! -e "$MESH" ] && [ ! -e "$MESH.pub" ]; then sudo -H -u "$SSH_USER" ssh-keygen -q -t ed25519 -N '' -f "$MESH"; fi
[ -s "$MESH" ] && [ -s "$MESH.pub" ] || die 'Node SSH key pair is incomplete'
PUB="$(cat "$MESH.pub")"
grep -Fqx -- "$PUB" "$AUTH" || printf '%s\n' "$PUB" >> "$AUTH"
chown "$SSH_USER:$GROUP" "$AUTH"; chmod 600 "$AUTH"
# /run is temporary; the resume unit can start before ssh.service creates this
# privilege-separation directory on a freshly booted minimal Ubuntu system.
install -d -m 755 /run/sshd
if [ ! -e "$STATE_DIR/ssh-host-keys-unique" ] && [ ! -e "$STATE_DIR/ready" ]; then
  say 'Generating unique SSH host keys for this node'
  HOST_KEY_BACKUP="$STATE_DIR/ssh-host-keys-before-prepare"
  install -d -m 700 "$HOST_KEY_BACKUP"
  for f in /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; do
    if [ -e "$f" ]; then cp -a "$f" "$HOST_KEY_BACKUP/"; fi
  done
  rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
  if ! ssh-keygen -A || ! sshd -t || ! systemctl restart ssh; then
    rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
    for f in "$HOST_KEY_BACKUP"/*; do if [ -e "$f" ]; then cp -a "$f" /etc/ssh/; fi; done
    systemctl restart ssh || true
    die 'Could not generate usable SSH host keys; previous keys were restored'
  fi
  for host in 127.0.0.1 localhost "$NAME" "$IP"; do
    sudo -H -u "$SSH_USER" ssh-keygen -R "$host" >/dev/null 2>&1 || true
  done
  rm -f "$HOST_KEY_BACKUP"/*
  rmdir "$HOST_KEY_BACKUP"
  touch "$STATE_DIR/ssh-host-keys-unique"
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
fi
systemctl enable --now ssh
sudo -H -u "$SSH_USER" ssh -i "$MESH" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SSH_USER@127.0.0.1" true || die 'Local SSH public-key login failed; password login remains enabled'
DROP=/etc/ssh/sshd_config.d/01-k8s-key-only.conf
cat > "$DROP" <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sshd -t
sshd -T | grep -qx 'passwordauthentication no' || die 'sshd password authentication setting is not effective'
sshd -T | grep -qx 'kbdinteractiveauthentication no' || die 'sshd keyboard-interactive authentication setting is not effective'
systemctl reload ssh
sudo -H -u "$SSH_USER" ssh -i "$MESH" -o IdentitiesOnly=yes -o BatchMode=yes "$SSH_USER@127.0.0.1" true || die 'Public-key login failed after disabling password login'
# The temporary grant allows a noninteractive, reboot-resumable phase two.
printf '%s ALL=(root) NOPASSWD:ALL\n' "$SSH_USER" > /etc/sudoers.d/90-k8s-bootstrap
chmod 440 /etc/sudoers.d/90-k8s-bootstrap
visudo -cf /etc/sudoers >/dev/null
sudo -u "$SSH_USER" sudo -n true || die 'Temporary passwordless sudo for phase 2 is not effective'
publish_manifest
rm -f "$STATE_DIR/reboot-pending"
touch "$STATE_DIR/ready"
systemctl disable k8s-prepare-resume.service >/dev/null
start_enrollment
say "Phase 1 complete: $NAME $IP. Use this node's private-network enrollment code during phase 2."
