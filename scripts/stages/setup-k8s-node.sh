#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Atlas KubeKit | By Atlas Richie
#
# setup-k8s-node.sh —— 第二阶段的 Kubernetes 节点配置脚本
#
# 涵盖阶段：
#   1. 基础依赖安装
#   2. hostname / /etc/hosts
#   3. netplan 静态 IP（netplan generate 先校验，再 apply）
#   4. 关闭 swap（kubelet 硬性要求）
#   5. 内核模块 + sysctl
#   6. containerd 安装 + cgroup driver 修复（containerd 1.x/2.x 通吃）
#   7. qemu-guest-agent
#   8. 数据盘：cp -> /var/lib/etcd   worker -> /os_data   lb -> 跳过
#   9. kubeadm/kubelet/kubectl（lb 跳过）
#
# 用法：
#   由 02-deploy-cluster.sh 使用 --k8s-only --yes 调用；保留旧的网络模式供维护。
#
# 例：
#   sudo bash setup-k8s-node.sh 10.20.1.22 cp1 cp
#   sudo bash setup-k8s-node.sh 10.20.1.25 w1 worker
#   sudo bash setup-k8s-node.sh 10.20.1.20 lb1 lb
#
# 幂等：可重复执行。每阶段先探测现状，已满足则标记 SKIP。
# 安全：数据盘阶段会执行 mkfs；--yes 必须同时指定 DATA_DEVICE=/dev/...

set -euo pipefail

# ============================================================== 可调参数
K8S_MINOR="${K8S_MINOR:-1.36}"          # Kubernetes 次版本
K8S_VERSION="${K8S_VERSION:-1.36.5}"     # 固定 patch 版本，避免新装机器随仓库漂移
K8S_DEB_VERSION="${K8S_DEB_VERSION:-1.36.5-1.1}"
K8S_PKG_CHANNEL="${K8S_PKG_CHANNEL:-stable}"
IFACE="${IFACE:-enp6s18}"               # 网卡名，各机一致
GATEWAY="${GATEWAY:-10.20.1.1}"
SUBNET="${SUBNET:-24}"
DNS1="${DNS1:-10.20.1.1}"
DNS2="${DNS2:-223.5.5.5}"
DATA_FSTYPE="${DATA_FSTYPE:-xfs}"       # xfs 对 etcd 更合适
K8S_TEST_MODE="${K8S_TEST_MODE:-0}"
NETPLAN_FILE="/etc/netplan/01-k8s.yaml"

# ============================================================== 参数解析
FULLIP="${1:-}"
NAME="${2:-}"
ROLE="${3:-}"
ASSUME_YES=0
NETWORK_ONLY=0
K8S_ONLY=0
for a in "${@:4}"; do
  case "$a" in
    --yes) ASSUME_YES=1 ;;
    --network-only) NETWORK_ONLY=1 ;;
    --k8s-only) K8S_ONLY=1 ;;
    *) printf '[ERROR] Unknown argument: %s\n' "$a" >&2; exit 1 ;;
  esac
done
[ "$NETWORK_ONLY" -eq 0 ] || [ "$K8S_ONLY" -eq 0 ] || { echo 'Cannot combine --network-only with --k8s-only' >&2; exit 2; }

die()  { printf '\033[31m[ERROR] %s\033[0m\n' "$*"; exit 1; }
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
inf()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[WARN] %s\033[0m\n' "$*"; }
pass() { printf '  \033[32mOK\033[0m %s\n' "$*"; }
skip() { printf '  \033[90m-\033[0m %s (already satisfied)\n' "$*"; }
hdr()  { printf '\n\033[1;36m===== %s =====\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "Run as root: sudo bash $0 <IP> <hostname> <role>"
[ -n "$FULLIP" ] || die "Usage: $0 <IP> <hostname> <lb|cp|worker> [--yes]"
[ -n "$NAME" ]   || die "Usage: $0 <IP> <hostname> <lb|cp|worker> [--yes]"
[ -n "$ROLE" ]   || die "Usage: $0 <IP> <hostname> <lb|cp|worker> [--yes]"

case "$ROLE" in
  lb|cp|worker) ;;
  *) die "Role must be lb, cp, or worker; got: $ROLE" ;;
esac
case "$K8S_TEST_MODE" in
  0) ;;
  1) ;;
  *) die 'K8S_TEST_MODE must be 0 or 1' ;;
esac
case "$FULLIP" in *.*.*.*) ;; *) die "Invalid IP format: $FULLIP" ;; esac
case "$NAME" in *[!a-z0-9-]*) die "Hostname must contain only lowercase letters, digits, and hyphens: $NAME" ;; esac

# 数据盘挂载点：lb 不需要
MOUNTPOINT=""
MIN_DISK_KB=0
case "$ROLE" in
  cp)     MOUNTPOINT="/var/lib/etcd"; MIN_DISK_KB=$((90 * 1024 * 1024)) ;;  # >= 90G
  worker) MOUNTPOINT="/os_data";     MIN_DISK_KB=$((500 * 1024 * 1024)) ;; # >= 500G
  lb)     MOUNTPOINT="" ;;
esac
if [ "$K8S_TEST_MODE" -eq 1 ] && [ "$ROLE" != lb ]; then
  MIN_DISK_KB=$((20 * 1024 * 1024))
fi

grn "============================================================"
grn " Node setup: $NAME  IP=$FULLIP  role=$ROLE"
grn "============================================================"

# ============================================================== 0. 前置校验
hdr "0. Preflight checks"
ip -o link show "$IFACE" >/dev/null 2>&1 \
  || die "Interface $IFACE not found; inspect ip -br link and rerun with IFACE=<name>"
pass "Network interface $IFACE exists"

# ============================================================== 1. 基础依赖
hdr "1. Base dependencies"
export DEBIAN_FRONTEND=noninteractive
inf "apt update"
if [ "$NETWORK_ONLY" -eq 0 ]; then
  apt-get update -qq || die "apt update failed; stopping before package installation"
  inf "Installing base packages"
  apt-get install -y -qq \
    curl wget git net-tools bind9-dnsutils lsb-release gnupg \
    iputils-ping netcat-openbsd socat conntrack ethtool openssh-client openssh-server \
    xfsprogs 2>/dev/null || apt-get install -y -qq \
    curl wget git net-tools bind9-dnsutils lsb-release gnupg \
    iputils-ping netcat-openbsd socat conntrack ethtool openssh-client openssh-server
  systemctl enable --now ssh >/dev/null || die "OpenSSH failed to start"
  pass "Base packages are ready"
fi

# 时间同步：K8s 证书校验与 etcd 租约都依赖时钟一致
if [ "$NETWORK_ONLY" -eq 1 ]; then
  skip "Time sync is deferred during network-only setup"
elif systemctl is-active --quiet systemd-timesyncd 2>/dev/null; then
  skip "Time sync: systemd-timesyncd is running"
elif systemctl is-active --quiet chrony 2>/dev/null; then
  skip "Time sync: chrony is running"
else
  inf "Installing chrony"
  apt-get install -y -qq chrony >/dev/null 2>&1 && systemctl enable --now chrony >/dev/null 2>&1 \
    && pass "chrony is running" || warn "chrony failed to install or start; check clock synchronization"
fi

# ============================================================== 2. hostname
if [ "$K8S_ONLY" -eq 1 ]; then
  [ "$(hostname)" = "$NAME" ] || die "Hostname must be $NAME; complete 01-prepare-node.sh first"
  ip -o -4 addr show "$IFACE" | grep -Fq " $FULLIP/" || die "Interface $IFACE lacks $FULLIP; complete 01-prepare-node.sh first"
else
hdr "2. hostname"
if [ -d /etc/cloud/cloud.cfg.d ]; then
  CLOUD_CFG=/etc/cloud/cloud.cfg.d/99-k8s-static-identity.cfg
  CLOUD_WANTED='preserve_hostname: true
network: {config: disabled}'
  if [ "$(cat "$CLOUD_CFG" 2>/dev/null || true)" != "$CLOUD_WANTED" ]; then
    printf '%s\n' "$CLOUD_WANTED" > "$CLOUD_CFG"
    pass "cloud-init will preserve hostname and static network"
  fi
fi
if [ "$(hostname)" = "$NAME" ]; then
  skip "Hostname is $NAME"
else
  inf "Setting hostname to $NAME"
  hostnamectl set-hostname "$NAME"
  pass "hostname = $(hostname)"
fi
if grep -qE "^127\.0\.1\.1[[:space:]]+$NAME([[:space:]]|$)" /etc/hosts; then
  skip "/etc/hosts maps 127.0.1.1 to $NAME"
else
  if grep -qE '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
    sed -i -E "s/^127\\.0\\.1\\.1[[:space:]].*$/127.0.1.1 $NAME/" /etc/hosts
  else
    printf '127.0.1.1 %s\n' "$NAME" >> /etc/hosts
  fi
  pass "/etc/hosts maps 127.0.1.1 to $NAME"
fi

# ============================================================== 3. 静态 IP
hdr "3. Static IP"
CURRENT_IP="$(ip -o -4 addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"
if [ "$CURRENT_IP" = "$FULLIP" ] \
  && grep -Fq "$IFACE:" "$NETPLAN_FILE" 2>/dev/null \
  && grep -Fq "addresses: [$FULLIP/$SUBNET]" "$NETPLAN_FILE" \
  && grep -Fq "routes: [{to: default, via: $GATEWAY}]" "$NETPLAN_FILE" \
  && grep -Fq "addresses: [$DNS1, $DNS2]" "$NETPLAN_FILE" \
  && grep -Fq 'dhcp4: false' "$NETPLAN_FILE" \
  && grep -Fq 'dhcp6: false' "$NETPLAN_FILE" \
  && grep -Fq 'accept-ra: false' "$NETPLAN_FILE" \
  && grep -Fq 'link-local: []' "$NETPLAN_FILE"; then
  skip "Static IP is $FULLIP/$SUBNET"
else
  inf "Backing up and writing netplan"
  netplan_had_file=0
  if [ -f "$NETPLAN_FILE" ]; then
    NETPLAN_BACKUP="${NETPLAN_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$NETPLAN_FILE" "$NETPLAN_BACKUP"
    netplan_had_file=1
  fi
  disabled_orig=()
  disabled_path=()
  # 归档其他 netplan 文件：多文件会合并作用在同一接口，行为不可预测
  for f in /etc/netplan/*.yaml; do
    [ -e "$f" ] || continue
    [ "$f" = "$NETPLAN_FILE" ] && continue
    moved="${f}.disabled-$(date +%Y%m%d-%H%M%S)"
    mv "$f" "$moved"
    disabled_orig+=("$f")
    disabled_path+=("$moved")
    warn "Disabled conflicting netplan file: $f"
  done
  cat > "$NETPLAN_FILE" <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      addresses: [$FULLIP/$SUBNET]
      routes: [{to: default, via: $GATEWAY}]
      nameservers:
        addresses: [$DNS1, $DNS2]
EOF
  chmod 600 "$NETPLAN_FILE"
  inf "Validating netplan syntax"
  if ! netplan generate; then
    rm -f "$NETPLAN_FILE"
    [ "$netplan_had_file" -eq 0 ] || cp -a "$NETPLAN_BACKUP" "$NETPLAN_FILE"
    for ((i=0; i<${#disabled_orig[@]}; i++)); do mv "${disabled_path[$i]}" "${disabled_orig[$i]}"; done
    die "Invalid netplan syntax; original configuration restored"
  fi
  pass "Syntax validation passed"
  inf "netplan apply"
  netplan apply || die "netplan apply failed; inspect console and backup ${NETPLAN_BACKUP:-none}"
  pass "Network configuration applied"
fi
sleep 1
ACTUAL_IP="$(ip -o -4 addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"
[ "$ACTUAL_IP" = "$FULLIP" ] || die "Expected IP $FULLIP, got $ACTUAL_IP; inspect ip -br addr"
ip route | grep -q "default via $GATEWAY" && pass "Default route is correct" || die "Default route missing: $(ip route | head -3)"
if [ "$NETWORK_ONLY" -eq 1 ]; then
  pass "Network setup complete; reconnect to $FULLIP to continue"
  exit 0
fi
fi # phase-one hostname/network section

# ============================================================== 4. swap
hdr "4. Disable swap"
if [ "$(free | awk '/^Swap:/{print $2}')" = "0" ]; then
  skip "Swap is already disabled"
else
  inf "swapoff -a"
  swapoff -a || warn "swapoff failed"
  if grep -qE '^[^#].*\sswap\s' /etc/fstab; then
    sed -i -E 's/^([^#].*\sswap\s)/\#\1/' /etc/fstab
    pass "Swap entry commented out in /etc/fstab"
  else
    pass "No swap entry in /etc/fstab"
  fi
  [ "$(free | awk '/^Swap:/{print $2}')" = "0" ] && pass "Swap is disabled" || warn "Swap is still active"
fi

# ============================================================== 5. 内核模块 / sysctl
hdr "5. Kernel modules and sysctl"
if grep -qs '^overlay$' /etc/modules-load.d/k8s.conf 2>/dev/null; then
  skip "modules-load.d/k8s.conf exists"
else
  printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/k8s.conf
  pass "Wrote /etc/modules-load.d/k8s.conf"
fi
modprobe overlay 2>/dev/null || warn "modprobe overlay failed"
modprobe br_netfilter 2>/dev/null || warn "modprobe br_netfilter failed"
lsmod | grep -q '^overlay' && pass "overlay loaded" || warn "overlay is not loaded"
lsmod | grep -q '^br_netfilter' && pass "br_netfilter loaded" || warn "br_netfilter is not loaded"

SYSCTL_FILE="/etc/sysctl.d/99-kubernetes.conf"
EXPECT_SYSCTL="net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1"
if [ "$(cat "$SYSCTL_FILE" 2>/dev/null | tr -d ' ')" = "$(printf '%s' "$EXPECT_SYSCTL" | tr -d ' ')" ]; then
  skip "sysctl configuration is current"
else
  printf '%s\n' "$EXPECT_SYSCTL" > "$SYSCTL_FILE"
  sysctl -p "$SYSCTL_FILE" >/dev/null || warn "sysctl -p failed"
  pass "Wrote and loaded $SYSCTL_FILE"
fi
for k in net.ipv4.ip_forward net.bridge.bridge-nf-call-iptables; do
  v="$(sysctl -n "$k" 2>/dev/null || echo '?')"
  [ "$v" = "1" ] && pass "$k = 1" || warn "$k = $v (expected 1)"
done

# ============================================================== 6. containerd
hdr "6. containerd + cgroup driver"
if ! command -v containerd >/dev/null 2>&1; then
  inf "Installing containerd"
  apt-get install -y -qq containerd || die "containerd installation failed"
  pass "containerd installed"
else
  skip "containerd installed: $(containerd --version | head -1)"
fi

CTD_RAW="$(containerd --version)"
# containerd --version 的输出格式不统一：
#   1.x: "containerd v1.7.24 ..."
#   2.x: "containerd github.com/containerd/containerd/v2 v2.2.1 ..."
# 2.x 的字符串里【第一个】 v2 后面跟的是空格不是点，用朴素的 sed 匹配会失败。
# 取最后一个 v<major>.<minor>.<patch> 形式的匹配。
# 解析不到也不致命 —— 下面的配置逻辑只认 runc options 的段名，
# 该段名在 1.x(io.containerd.grpc.v1.cri) 和 2.x(io.containerd.cri.v1.runtime) 都存在。
CTD_MAJOR="$(printf '%s' "$CTD_RAW" | grep -oE 'v[0-9]+(\.[0-9]+)*' | tail -n1 | cut -d. -f1 | tr -d 'v' || true)"
[ -n "$CTD_MAJOR" ] || CTD_MAJOR="unknown"
inf "containerd version: $CTD_RAW"
inf "Detected major version: $CTD_MAJOR"

CONFIG="/etc/containerd/config.toml"
if [ ! -s "$CONFIG" ]; then
  inf "Generating missing containerd default configuration"
  mkdir -p /etc/containerd
  containerd config default > "$CONFIG" || die "containerd config default failed"
else
  skip "Keeping existing $CONFIG"
fi
CFG_VERSION="$(head -1 "$CONFIG" | tr -d ' ')"
pass "Configuration version: $CFG_VERSION"

RUNC_SECTION_RE='runtimes\.runc\.options\]'
grep -q "$RUNC_SECTION_RE" "$CONFIG" || die "runc options section not found; cannot safely edit configuration (version: $CTD_MAJOR)"

# 一趟 awk：删除所有已存在的 SystemdCgroup 行，避免段外/重复定义；再在段头后插入 true
# 不用 sed 的 a\ —— 它的转义在 BSD/GNU sed 之间行为不一致
awk '
  /SystemdCgroup/               { next }
  { print }
  /runtimes\.runc\.options\]/  { print "    SystemdCgroup = true" }
' "$CONFIG" > "${CONFIG}.tmp"

# 段边界必须用 [[:space:]]*\[ —— containerd 生成的嵌套段头是带缩进的，
# 用 ^\[ 会把后续段一起吞进块里，导致断言形同虚设
runc_block() { awk '/runtimes\.runc\.options\]/{f=1;next} /^[[:space:]]*\[/{f=0} f' "$1"; }

runc_block "${CONFIG}.tmp" | grep -qE '^[[:space:]]*SystemdCgroup[[:space:]]*=[[:space:]]*true[[:space:]]*$' \
  || die "SystemdCgroup = true is missing from runc options section. Section:
$(runc_block "${CONFIG}.tmp")"

TOT="$(grep -c 'SystemdCgroup' "${CONFIG}.tmp" || true)"
INB="$(runc_block "${CONFIG}.tmp" | grep -c 'SystemdCgroup' || true)"
[ "$TOT" = "$INB" ] || die "Duplicate SystemdCgroup entries outside runc options (total=$TOT in-section=$INB)"
pass "SystemdCgroup = true is inside runc options without duplicates"

if ! cmp -s "${CONFIG}.tmp" "$CONFIG"; then
  cp -a "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
  mv "${CONFIG}.tmp" "$CONFIG"
  chmod 644 "$CONFIG"
  inf "containerd configuration changed; restarting"
  systemctl restart containerd || die "containerd restart failed: journalctl -u containerd -n 50 --no-pager"
  sleep 2
else
  rm -f "${CONFIG}.tmp"
  skip "containerd SystemdCgroup configuration is current"
fi
systemctl is-active --quiet containerd || die "containerd is not active"
systemctl enable containerd >/dev/null 2>&1 || true
pass "containerd is running and enabled"

# ============================================================== 7. guest agent
hdr "7. qemu-guest-agent"
if systemctl is-active --quiet qemu-guest-agent 2>/dev/null; then
  skip "Already running"
else
  inf "Installing qemu-guest-agent"
  apt-get install -y -qq qemu-guest-agent >/dev/null 2>&1 || warn "Installation failed"
  systemctl enable --now qemu-guest-agent >/dev/null 2>&1 || true
  if systemctl is-active --quiet qemu-guest-agent 2>/dev/null; then
    pass "qemu-guest-agent is running"
  else
    warn "qemu-guest-agent is not running; enable the VirtIO serial port in PVE"
  fi
fi

# ============================================================== 8. 数据盘
hdr "8. Data disk"
# `lsblk -s` prints Unicode tree glyphs before ancestor names on some util-linux
# versions. Strip everything outside the device-name alphabet instead of relying
# on POSIX whitespace classes to remove multi-byte glyphs.
chain_of() { lsblk -sno NAME "$1" 2>/dev/null | sed 's/[^A-Za-z0-9._-]//g' | sed '/^$/d' | sed 's#^#/dev/#'; }

if [ -z "$MOUNTPOINT" ]; then
  skip "LB requires no data disk"
elif [ "${DATA_DEVICE:-}" = none ]; then
  if mountpoint -q "$MOUNTPOINT" 2>/dev/null || awk -v mp="$MOUNTPOINT" '$1 !~ /^#/ && $2 == mp {found=1} END {exit !found}' /etc/fstab; then
    die "DATA_DEVICE=none conflicts with an existing mount or fstab entry for $MOUNTPOINT"
  fi
  ROOT_FREE_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
  [ -n "$ROOT_FREE_KB" ] && [ "$ROOT_FREE_KB" -ge 10485760 ] || die 'At least 10 GiB of free system disk space is required without a separate data disk'
  pass "No separate data disk; $MOUNTPOINT and Kubernetes node data will use the system disk"
elif mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
  MOUNT_SRC="$(findmnt -no SOURCE "$MOUNTPOINT")"
  ROOT_SRC="$(findmnt -no SOURCE /)"
  ROOT_DISK="$(chain_of "$ROOT_SRC" | tail -n 1)"
  DATA_DISK="$(chain_of "$MOUNT_SRC" | tail -n 1)"
  [ -n "$DATA_DISK" ] || die "Cannot resolve device chain for $MOUNTPOINT"
  [ "$ROOT_DISK" != "$DATA_DISK" ] || die "$MOUNTPOINT shares the root disk ($DATA_DISK); a separate disk is required"
  MOUNT_KB="$(df -Pk "$MOUNTPOINT" | awk 'NR==2 {print $2}')"
  [ -n "$MOUNT_KB" ] && [ "$MOUNT_KB" -ge "$MIN_DISK_KB" ] || die "$MOUNTPOINT is too small for role=$ROLE or its size cannot be read"
  pass "$MOUNTPOINT is mounted on separate disk: $MOUNT_SRC"
elif awk -v mp="$MOUNTPOINT" '$1 !~ /^#/ && $2 == mp {found=1} END {exit !found}' /etc/fstab; then
  inf "Mounting $MOUNTPOINT from existing fstab entry (no formatting)"
  mount "$MOUNTPOINT" || die "Cannot mount $MOUNTPOINT from fstab; no disk will be formatted"
  mountpoint -q "$MOUNTPOINT" || die "$MOUNTPOINT is still not mounted"
  MOUNT_SRC="$(findmnt -no SOURCE "$MOUNTPOINT")"
  ROOT_SRC="$(findmnt -no SOURCE /)"
  ROOT_DISK="$(chain_of "$ROOT_SRC" | tail -n 1)"
  DATA_DISK="$(chain_of "$MOUNT_SRC" | tail -n 1)"
  [ -n "$DATA_DISK" ] || die "Cannot resolve device chain for $MOUNTPOINT"
  [ "$ROOT_DISK" != "$DATA_DISK" ] || die "$MOUNTPOINT shares the root disk ($DATA_DISK); a separate disk is required"
  MOUNT_KB="$(df -Pk "$MOUNTPOINT" | awk 'NR==2 {print $2}')"
  [ -n "$MOUNT_KB" ] && [ "$MOUNT_KB" -ge "$MIN_DISK_KB" ] || die "$MOUNTPOINT is too small for role=$ROLE or its size cannot be read"
  pass "$MOUNTPOINT mounted from fstab: $(findmnt -no SOURCE "$MOUNTPOINT")"
else
  ROOT_SRC="$(findmnt -no SOURCE /)"
  ROOT_DISK="$(chain_of "$ROOT_SRC" | tail -n 1)"
  [ -n "$ROOT_DISK" ] || die "Cannot resolve root device chain for $ROOT_SRC; stopping"
  if [ -n "${DATA_DEVICE:-}" ]; then
    [ -b "$DATA_DEVICE" ] || die "DATA_DEVICE is not a block device: $DATA_DEVICE"
    DATA_DEVICE="$(readlink -f "$DATA_DEVICE")"
  fi

  inf "Root partition $ROOT_SRC -> disk $ROOT_DISK"

  # 候选 = 整盘、非根盘、无分区、无文件系统、未挂载
  CANDIDATES=""
  while read -r name; do
    [ -n "$name" ] || continue
    dev="/dev/$name"
    [ "$dev" = "$ROOT_DISK" ] && continue
    [ "$(lsblk -nr -o NAME "$dev" 2>/dev/null | wc -l)" -gt 1 ] && continue   # 有分区 -> 跳过
    [ -n "$(blkid -o value -s TYPE "$dev" 2>/dev/null || true)" ] && continue  # 有文件系统 -> 跳过
    signatures="$(wipefs -n "$dev" 2>/dev/null)" || continue
    [ -z "$signatures" ] || continue   # 其他文件系统/RAID 签名 -> 跳过
    findmnt -n | awk '{print $1}' | grep -qx "$dev" && continue               # 已挂载 -> 跳过
    CANDIDATES="$CANDIDATES $dev"
  done < <(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print $1}')

  CNT=$(echo "$CANDIDATES" | wc -w)
  if [ "$CNT" -eq 0 ]; then
    die "No blank data disk found; add an unpartitioned, unformatted disk in PVE"
  fi
  if [ -n "${DATA_DEVICE:-}" ]; then
    case " $CANDIDATES " in *" $DATA_DEVICE "*) DEV="$DATA_DEVICE" ;; *) die "DATA_DEVICE=$DATA_DEVICE is not a blank disk candidate: $CANDIDATES" ;; esac
  else
    [ "$CNT" -eq 1 ] || die "Multiple blank disks found: $CANDIDATES; set DATA_DEVICE=/dev/..."
    DEV="$(echo "$CANDIDATES" | tr -d ' ')"
  fi
  if [ "$ASSUME_YES" -eq 1 ]; then
    [ -n "${DATA_DEVICE:-}" ] || die "Noninteractive formatting requires DATA_DEVICE=/dev/..."
    [ "$DATA_DEVICE" = "$DEV" ] || die "DATA_DEVICE=$DATA_DEVICE does not match candidate $DEV"
  fi
  DEV_DISK="$(chain_of "$DEV" | tail -n 1)"
  DEV_SIZE_KB="$(lsblk -bndo SIZE "$DEV" 2>/dev/null | awk '{printf "%d", $1/1024}')"
  inf "Selected disk: $DEV  whole disk=$DEV_DISK  size=$((DEV_SIZE_KB / 1024 / 1024)) GiB"

  if [ "$DEV_DISK" = "$ROOT_DISK" ]; then
    die "$DEV is on the root disk ($DEV_DISK); formatting would erase the OS"
  fi
  pass "Disk separation checked: root=$ROOT_DISK target=$DEV_DISK"

  if [ -n "$DEV_SIZE_KB" ] && [ "$DEV_SIZE_KB" -lt "$MIN_DISK_KB" ]; then
    die "$DEV size $((DEV_SIZE_KB / 1024 / 1024)) GiB is below the $((MIN_DISK_KB / 1024 / 1024)) GiB required for role=$ROLE.
       Check the disk mapping and DATA_DEVICE."
  fi
  [ -n "$MIN_DISK_KB" ] && pass "Disk size meets requirement (>= $((MIN_DISK_KB / 1024 / 1024)) GiB)"

  echo
  red "  WARNING: mkfs.$DATA_FSTYPE will erase all data on $DEV."
  red "  The disk will be mounted at $MOUNTPOINT."
  echo

  if [ "$ASSUME_YES" -eq 0 ]; then
    if [ -t 0 ]; then
      printf "Type yes to format the disk: "
      read -r ans
      [ "$ans" = "yes" ] || die "Cancelled; no changes made"
    else
      die "Noninteractive session requires --yes and an explicit DATA_DEVICE"
    fi
  fi

  inf "Formatting $DEV as $DATA_FSTYPE"
  command -v "mkfs.$DATA_FSTYPE" >/dev/null || die "mkfs.$DATA_FSTYPE not found"
  "mkfs.$DATA_FSTYPE" -f "$DEV"

  UUID="$(blkid -s UUID -o value "$DEV")"
  [ -n "$UUID" ] || die "Cannot read UUID; mount aborted"
  mkdir -p "$MOUNTPOINT"
  # 写 UUID 而不是 /dev/sdX：以后加删盘会导致设备名漂移，写死会静默写错盘
  if ! grep -q "^UUID=$UUID " /etc/fstab; then
    if [ "$DATA_FSTYPE" = "xfs" ]; then FSPASS="0 0"; else FSPASS="0 2"; fi
    echo "UUID=$UUID $MOUNTPOINT $DATA_FSTYPE defaults,noatime $FSPASS" >> /etc/fstab
  fi
  mount "$MOUNTPOINT" || die "Mount failed"
  [ "$(findmnt -no UUID "$MOUNTPOINT")" = "$UUID" ] || { umount "$MOUNTPOINT"; die "UUID verification failed; unmounted"; }
  pass "Mounted: $(findmnt -no SOURCE "$MOUNTPOINT") -> $MOUNTPOINT"
  warn "Reboot and verify fstab with: findmnt $MOUNTPOINT"
fi

# ============================================================== 9. K8s 组件
hdr "9. kubeadm / kubelet / kubectl"
if [ "$ROLE" = "lb" ]; then
  skip "LB does not need Kubernetes node packages"
else
  KUBEADM_PKG="$(dpkg-query -W -f='${Version}' kubeadm 2>/dev/null || true)"
  KUBELET_PKG="$(dpkg-query -W -f='${Version}' kubelet 2>/dev/null || true)"
  KUBECTL_PKG="$(dpkg-query -W -f='${Version}' kubectl 2>/dev/null || true)"
  for pkg_version in "$KUBEADM_PKG" "$KUBELET_PKG" "$KUBECTL_PKG"; do
    [ -z "$pkg_version" ] || [ "$pkg_version" = "$K8S_DEB_VERSION" ] \
      || die "Installed Kubernetes package version $pkg_version differs from target $K8S_DEB_VERSION; upgrade manually"
  done
  if [ "$KUBEADM_PKG" = "$K8S_DEB_VERSION" ] && [ "$KUBELET_PKG" = "$K8S_DEB_VERSION" ] && [ "$KUBECTL_PKG" = "$K8S_DEB_VERSION" ]; then
    skip "kubeadm, kubelet, and kubectl are ${K8S_DEB_VERSION}"
  else
    inf "Configuring pkgs.k8s.io repository (v${K8S_MINOR})"
    apt-get install -y -qq apt-transport-https ca-certificates gnupg >/dev/null
    install -d -m 755 /etc/apt/keyrings
    if [ ! -s /etc/apt/keyrings/kubernetes-apt-keyring.gpg ]; then
      key_tmp="$(mktemp)"
      curl -fsSL "https://pkgs.k8s.io/core:/${K8S_PKG_CHANNEL}:/v${K8S_MINOR}/deb/Release.key" \
        | gpg --dearmor > "$key_tmp"
      install -m 644 "$key_tmp" /etc/apt/keyrings/kubernetes-apt-keyring.gpg
      rm -f "$key_tmp"
    fi
    repo_line="deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/${K8S_PKG_CHANNEL}:/v${K8S_MINOR}/deb/ /"
    if [ "$(cat /etc/apt/sources.list.d/kubernetes.list 2>/dev/null || true)" != "$repo_line" ]; then
      printf '%s\n' "$repo_line" > /etc/apt/sources.list.d/kubernetes.list
    fi
    apt-get update -qq || die "apt update failed"
    inf "Installing kubelet, kubeadm, and kubectl"
    apt-get install -y -qq "kubelet=${K8S_DEB_VERSION}" "kubeadm=${K8S_DEB_VERSION}" "kubectl=${K8S_DEB_VERSION}" || die "Kubernetes package installation failed; target version ${K8S_DEB_VERSION} may be unavailable"
    # hold：K8s 升级必须用 kubeadm upgrade 一次一个 minor，apt 自动升级会破坏版本纪律
    apt-mark hold kubelet kubeadm kubectl
    pass "Installed and held packages: $(kubeadm version -o short 2>/dev/null)"
  fi
  systemctl enable kubelet >/dev/null 2>&1 || true
  inf "kubelet enabled at boot (restarts before kubeadm init are expected)"
fi

# ============================================================== 汇总
hdr "Setup summary"
printf '  hostname   : %s\n' "$(hostname)"
printf '  IP         : %s\n' "$(ip -o -4 addr show "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)"
printf '  default route: %s\n' "$(ip route | grep '^default' | head -1)"
printf '  kernel     : %s\n' "$(uname -r)"
printf '  memory     : %s\n' "$(free -h | awk '/^Mem:/{print $2}')"
printf '  swap       : %s\n' "$(free -h | awk '/^Swap:/{print $2}')"
printf '  containerd : %s\n' "$(containerd --version | head -1)"
printf '  kubeadm    : %s\n' "$(kubeadm version -o short 2>/dev/null || echo 'not installed (expected on LB)')"
printf '  product_uuid: %s\n' "$(cat /sys/class/dmi/id/product_uuid 2>/dev/null || echo 'unavailable')"
printf '  machine-id : %s\n' "$(cat /etc/machine-id 2>/dev/null || echo 'unavailable')"
echo
grn "============================================================"
grn " Automated setup complete. Manual checks:"
grn "============================================================"
echo "  1. product_uuid and machine-id must be unique across all registered nodes."
echo "     The phase-2 orchestrator checks these before cluster initialization."
echo
echo "  2. If this node has a data disk, verify fstab after reboot:"
if [ -n "$MOUNTPOINT" ] && [ "${DATA_DEVICE:-}" != none ]; then
  echo "       findmnt $MOUNTPOINT && df -hT $MOUNTPOINT"
else
  echo "       (no separate data disk configured)"
fi
echo
if [ "$ROLE" = cp ] && [ "${DATA_DEVICE:-}" != none ]; then
  echo "  3. The CP data disk must stay mounted at /var/lib/etcd before kubeadm init."
  echo "     A later mount would hide data written earlier."
else
  echo "  3. Kubernetes node data uses the selected filesystem layout."
fi
echo
echo "  4. Allow ports 6443, 2379-2380, 10250, 10257, 10259, and"
echo "     Cilium 8472/4240 between nodes; inspect ufw status."
