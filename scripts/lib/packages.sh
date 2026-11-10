#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# APT coordination. Sourcing this module only defines functions.

atlas_wait_locks() {
  local timeout="$1" message="$2"
  shift 2
  python3 - "$timeout" "$message" "$@" <<'PY'
import fcntl, os, sys, time
timeout=int(sys.argv[1])
if not 1<=timeout<=3600:raise SystemExit(2)
deadline=time.monotonic()+timeout
reported=False
while True:
    blocked=False
    for path in sys.argv[3:]:
        descriptor=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
        try:
            try:fcntl.lockf(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:blocked=True
        finally:os.close(descriptor)
    if not blocked:break
    if not reported:
        print(sys.argv[2],file=sys.stderr,flush=True);reported=True
    if time.monotonic()>=deadline:raise SystemExit(75)
    time.sleep(1)
PY
}

atlas_apt() {
  local timeout="${ATLAS_PACKAGE_LOCK_TIMEOUT:-300}" message status
  if ! [[ "$timeout" =~ ^[0-9]+$ ]] || [ "$timeout" -lt 1 ] || [ "$timeout" -gt 3600 ]; then
    atlas_t common.package.invalid_timeout >&2; printf '\n' >&2; return 2
  fi
  message="$(atlas_t common.package.wait_lock)"
  if atlas_wait_locks "$timeout" "$message" \
    /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
    /var/lib/apt/lists/lock /var/cache/apt/archives/lock; then :
  else status=$?; atlas_t common.package.lock_timeout >&2; printf '\n' >&2; return "$status"; fi
  if [ -f /var/lib/k8s-deploy/materials.json ]; then
    python3 /usr/local/lib/k8s-deploy/scripts/operations/materials/runtime.py apt \
      -o "DPkg::Lock::Timeout=$timeout" -o Acquire::Retries=0 "$@"
  else
    apt-get -o "DPkg::Lock::Timeout=$timeout" -o Acquire::Retries=3 "$@"
  fi
}
