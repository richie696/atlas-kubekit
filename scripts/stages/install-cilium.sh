#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Atlas KubeKit | By Atlas Richie
set -euo pipefail
K8S_VERSION="${K8S_VERSION:-v1.36.5}"
CILIUM_VERSION="${CILIUM_VERSION:-1.20.2}"
HELM_VERSION="${HELM_VERSION:-v3.18.6}"
CILIUM_OPERATOR_REPLICAS="${CILIUM_OPERATOR_REPLICAS:-1}"
[[ "$CILIUM_OPERATOR_REPLICAS" =~ ^[12]$ ]] || { echo 'Operator replicas must be 1 or 2' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
command -v kubeadm >/dev/null || { echo 'Install Kubernetes node components first' >&2; exit 1; }
[ -s /etc/kubernetes/admin.conf ] || { echo 'Run this on initialized cp1' >&2; exit 1; }
export KUBECONFIG=/etc/kubernetes/admin.conf
actual="$(kubeadm version -o short)"
[ "$actual" = "$K8S_VERSION" ] || { echo "kubeadm $actual does not match target $K8S_VERSION" >&2; exit 1; }

if command -v helm >/dev/null 2>&1 && kubectl -n kube-system get daemonset cilium >/dev/null 2>&1; then
  release_ok="$(helm list -n kube-system --filter '^cilium$' -o json | python3 -c 'import json,sys; x=json.load(sys.stdin); print("yes" if x and x[0].get("status")=="deployed" and x[0].get("chart")=="cilium-'"$CILIUM_VERSION"'" else "no")')"
  values_ok="$(helm get values cilium -n kube-system -o json 2>/dev/null | python3 -c 'import json,sys; x=json.load(sys.stdin); print("yes" if x.get("ipam",{}).get("mode")=="kubernetes" and x.get("operator",{}).get("replicas")==int(sys.argv[1]) else "no")' "$CILIUM_OPERATOR_REPLICAS" || true)"
  if [ "$release_ok" = yes ] && [ "$values_ok" = yes ]; then
    kubectl -n kube-system rollout status daemonset/cilium --timeout=10m
    kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m
    echo "Cilium $CILIUM_VERSION is healthy; Helm upgrade skipped."
    exit 0
  fi
fi

if ! command -v helm >/dev/null 2>&1 || [ "$(helm version --template '{{.Version}}' 2>/dev/null || true)" != "$HELM_VERSION" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  arch=amd64
  case "$(dpkg --print-architecture)" in arm64) arch=arm64;; amd64) ;; *) echo 'Unsupported architecture' >&2; exit 1;; esac
  curl -fL --retry 3 "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o "$tmp/helm.tgz"
  curl -fL --retry 3 "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz.sha256sum" -o "$tmp/helm.sha256sum"
  expected="$(awk 'NR==1 {print $1}' "$tmp/helm.sha256sum")"
  actual_sum="$(sha256sum "$tmp/helm.tgz" | awk '{print $1}')"
  [ -n "$expected" ] && [ "$expected" = "$actual_sum" ] || { echo 'Helm download SHA256 verification failed' >&2; exit 1; }
  tar -xzf "$tmp/helm.tgz" -C "$tmp"
  install -m 755 "$tmp/linux-${arch}/helm" /usr/local/bin/helm
fi

tmp=$(mktemp -d)
trap 'rm -rf "${tmp:-}"' EXIT
helm pull oci://quay.io/cilium/charts/cilium --version "$CILIUM_VERSION" --destination "$tmp"
helm upgrade --install cilium "$tmp/cilium-${CILIUM_VERSION}.tgz" \
  --namespace kube-system --set ipam.mode=kubernetes --set "operator.replicas=${CILIUM_OPERATOR_REPLICAS}" \
  --wait --timeout 10m
kubectl -n kube-system rollout status daemonset/cilium --timeout=10m
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m
echo "Cilium $CILIUM_VERSION is ready."
