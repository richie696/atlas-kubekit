#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Native deployment acceptance is shared by first install and read-only replay.

atlas_deploy_verify_service_routes() {
  local node service_address
  service_address="$(python3 -c 'import ipaddress,sys; print(ipaddress.IPv4Network(sys.argv[1], strict=False).network_address + 1)' "$SVC_CIDR" 2>/dev/null)" \
    || die "$(atlas_t phase02.config.conflict)"
  for node in "${K8S_NODES[@]}"; do
    run "$node" "ip -4 route get $(q "$service_address") >/dev/null" \
      || die "$(atlas_printf phase02.error.service_route "$node" "$service_address")"
  done
}

atlas_deploy_verify_cluster() {
  local n etcd_rows dns_rows
  run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes -o wide'
  for n in "${K8S_NODES[@]}"; do
    run cp1 "KUBECONFIG=/etc/kubernetes/admin.conf kubectl wait --for=condition=Ready node/$(q "$n") --timeout=2m"
  done
  run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status deployment/coredns --timeout=5m'
  run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status deployment/cilium-operator --timeout=5m'
  run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system rollout status daemonset/cilium --timeout=5m'
  etcd_rows="$(run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system get pods -l component=etcd --no-headers')"
  printf '%s\n' "$etcd_rows" | awk -v expected="${#CPS[@]}" '$2=="1/1" && $3=="Running"{ok++} END{exit !(NR==expected && ok==expected)}' \
    || die "$(atlas_t phase02.error.etcd_layout)"
  dns_rows="$(run cp1 'KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide --no-headers')"
  printf '%s\n' "$dns_rows" | awk -v expected="${#CPS[@]}" -v names=" ${CPS[*]} " '$2=="1/1" && $3=="Running" && index(names," "$7" "){ok++; nodes[$7]=1} END{for(n in nodes) distinct++; exit !(NR==expected && ok==expected && distinct==expected)}' \
    || die "$(atlas_t phase02.error.dns_layout)"
  for n in "${LBS[@]}"; do
    run "$n" 'systemctl is-active --quiet haproxy && systemctl is-active --quiet keepalived && systemctl is-enabled --quiet haproxy && systemctl is-enabled --quiet keepalived && haproxy -c -f /etc/haproxy/haproxy.cfg && keepalived -t -f /etc/keepalived/keepalived.conf'
  done
  wait_lb_vip_owner "$VIP"
  if [ -n "$APP_INGRESS_VIP" ]; then wait_lb_vip_owner "$APP_INGRESS_VIP"; fi
  curl -kfsS --connect-timeout 5 "https://$VIP:$API_PORT/readyz" | grep -qx ok \
    || die "$(atlas_t phase02.error.api_health)"
}
