#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Atlas KubeKit | By Atlas Richie
set -euo pipefail

NAMESPACE=kube-system
DEPLOYMENT=coredns
LABEL_SELECTOR='k8s-app=kube-dns'
KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
export KUBECONFIG

[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo 'kubectl is missing' >&2; exit 1; }
kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" >/dev/null || {
  echo 'kube-system/coredns Deployment not found; complete kubeadm init and CNI installation first' >&2
  exit 1
}
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" >/dev/null || {
  echo 'Cannot query CoreDNS Pods; stopping' >&2
  exit 1
}

# Use every registered control-plane node (including temporarily NotReady nodes)
# so a transient outage does not scale CoreDNS down and forget that failure domain.
REPLICAS="$(kubectl get nodes -l node-role.kubernetes.io/control-plane -o name | awk 'END { print NR + 0 }')"
[[ "$REPLICAS" =~ ^[1-9][0-9]*$ ]] || { echo 'No registered control-plane nodes found' >&2; exit 1; }

PATCH="$(cat <<EOF
{
  "spec": {
    "replicas": ${REPLICAS},
    "template": {
      "spec": {
        "nodeSelector": {
          "node-role.kubernetes.io/control-plane": ""
        },
        "affinity": {
          "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
              {
                "labelSelector": {
                  "matchLabels": {
                    "k8s-app": "kube-dns"
                  }
                },
                "topologyKey": "kubernetes.io/hostname"
              }
            ]
          }
        }
      }
    }
  }
}
EOF
)"

kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" --type=merge -p "$PATCH"
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=10m
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o wide
echo "CoreDNS has ${REPLICAS} replicas on distinct control-plane nodes."
