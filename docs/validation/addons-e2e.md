# Atlas KubeKit — 阶段三插件 E2E 验证记录

本次验证在用户运行中的测试集群执行，必须通过 `03-install-addons.sh` 的交互路径安装，然后验证插件实际功能。仅 Helm deployed、Pod Ready 或 HTTP 健康检查不能视为完整功能通过。

**结果：2026-10-02，14/14 项核心功能 E2E 通过。** 共安装 15 个插件 Helm release（Loki 与 Alloy 分别安装），加上原有 Cilium 共 16 个。撤销临时拉取代理后，6 个节点仍 Ready，运行中的 Pod 容器全部 Ready，metrics API 与 external metrics API Available=True。

## 环境与范围

- 发起控制平面：cp1，10.20.1.22；cp2/cp3：10.20.1.23/24；w1/w2/w3：10.20.1.25/26/27。
- 基线：Kubernetes v1.36.5，Cilium 1.20.2，6 个 Kubernetes 节点 Ready；除 Cilium 外无 Helm release；无 StorageClass。
- 测试资源使用独立 namespace 和名称；现有集群资源保留。
- 测试可使用真实的临时 NFS、S3、DNS、Git 等依赖；不以模拟 Helm/kubectl 代替实际安装。
- MetalLB 的 Layer 2 地址须由用户确认预留后使用。真实公网 DNS/云服务商认证若未提供，不纳入通过范围。
- 记录 Chart/app 版本、安装结果、功能断言、故障原因和清理结果。小规格测试集群验证不代表生产容量或高可用验收。

## 验证矩阵

| 编号 | 插件 | 实际功能断言 | 依赖/测试资源 | 状态 |
|---|---|---|---|---|
| 01 | Metrics Server | metrics API 返回节点/Pod 指标，kubectl top 可用 | 活跃测试 Pod | 已通过 |
| 02 | cert-manager | 签发测试证书，Secret 含证书和私钥，证书有效 | 测试自签名 Issuer、Certificate | 已通过 |
| 03 | Prometheus stack | Prometheus 查询集群指标，Grafana 数据源可用，测试告警送达 Alertmanager | 测试 PrometheusRule | 已通过 |
| 04 | Loki + Alloy | 测试 Pod 日志经 Alloy 采集后可从 Loki 查询 | 测试日志 Pod、临时 Loki 存储 | 已通过 |
| 05 | Argo CD | 从 Git 同步测试应用，Git 变更在集群中生效 | 测试 Git repo/Application | 已通过 |
| 06 | External Secrets | 外部源生成 Secret，源更新同步到目标 Secret | 测试 provider/SecretStore | 已通过 |
| 07 | ExternalDNS | 创建路由/Service 后 DNS 服务中出现正确记录和 TXT 所有者记录 | 用户 DNS 或临时 RFC2136 DNS | 已通过 |
| 08 | Velero | 备份测试资源与数据，删除后恢复且内容一致 | 临时 S3、测试数据 | 已通过 |
| 09 | NFS CSI | PVC Bound；Pod 写入、重建后数据可读 | 临时或现有 NFS | 已通过 |
| 10 | Envoy Gateway | Gateway/Route Accepted；HTTP 正确转发；安全策略拒绝不符请求 | 测试后端/路由/策略 | 已通过 |
| 11 | Kyverno | 合规资源准入，不合规资源被拒绝 | 局限于测试 namespace 的策略 | 已通过 |
| 12 | MetalLB | 分配预留 IP，集群外可通过该 IP 访问测试 Service | 用户预留 IP、测试后端 | 已通过 |
| 13 | KEDA | 触发器导致工作负载扩容，条件结束后缩容 | 测试 ScaledObject | 已通过 |
| 14 | Traefik Ingress | 域名/路径转发、错误域名 404、TLS 证书及 hostname 校验 | 测试 Ingress/后端 | 已通过 |

## 执行证据

- Metrics Server：通过菜单 1 安装 Chart `metrics-server-3.14.0`，应用 `0.9.0`；6 个节点的 metrics API 和 `kubectl top nodes` 返回有效指标，测试应用 `kubectl top pods` 返回数据。测试集群通过菜单显式允许自签名 kubelet 的 insecure TLS，此结论不包含生产 kubelet TLS 验证。
- cert-manager：通过菜单 2 安装 Chart/app `v1.21.2`；测试 Issuer 签发 Certificate，Ready=True，生成 TLS Secret；OpenSSL 验证证书有效期并通过 `-checkend 3600`。
- 环境问题：Docker Hub 直连超时且节点 DNS 返回错误地址；DaoCloud 代理可拉取 busybox，但不能覆盖全部插件镜像。当前 E2E 临时转发已有网络代理，通过 lb2 转发供节点 containerd 和 cp1 Helm 拉取。测试结束需撤销临时代理配置，生产部署仍须自行具备可达镜像/Chart 网络。已撤销测试中的 GitHub Pages hosts 地址固定；不修改已有 `registry.k8s.io` 配置。
- 临时依赖：用户已确认使用测试服务，MetalLB 地址范围 `10.20.1.30-10.20.1.40`；lb2 提供独立 NFS 测试导出和 RFC2136 DNS（5353 端口），不覆盖系统 DNS 配置。


- NFS CSI：通过菜单 9 实际安装；RWX PVC Bound；Pod 写入固定测试内容，删除并重建 Pod 后仍读到相同内容。
- 测试 S3 依赖：MinIO 指定的官方镜像拉取返回 401/insufficient_scope，改用官方 RustFS 镜像提供真实 S3 API；不影响 Velero 插件菜单的安装路径。

- Prometheus stack：Chart `91.8.2`；查询 `kube_node_info` 返回 6 个节点；测试 PrometheusRule 触发的 `AddonE2EAlwaysFiring` 告警出现在 Alertmanager；使用 Secret 中的管理员认证后，Grafana Prometheus 数据源健康查询返回 OK。
- Argo CD：真实 `git://10.20.1.21:9418/app.git` 测试仓库初次同步生成 ConfigMap，再提交新的 Git revision 并验证集群内容从 initial 更新为 updated。测试 AppProject 仅允许本测试仓库、namespace 和 ConfigMap。
- External Secrets：Kubernetes provider 从隔离源 namespace 同步 Secret；源数据更新后目标 Secret 自动更新。此证据不覆盖云厂商 Secret Manager 的认证和权限。
- ExternalDNS：RFC2136 TSIG 更新 BIND；实际 DNS 查询得到 `lb.addon-e2e.test A 10.20.1.30`，`a-lb.addon-e2e.test TXT` 含 owner=addon-e2e。应用 v0.23.0 使用新的 `external-dns.kubernetes.io/hostname` annotation；测试初始使用旧 alpha annotation 未产生记录，修正后通过。
- MetalLB：菜单实际配置预留池；分配 10.20.1.30，ARP 宣告生效后从集群外 lb2 请求返回预期业务文本。首个请求发生在宣告生效前超时，随后真实外部请求通过。
- Envoy Gateway：Chart/app `v1.9.2`；Gateway Programmed=True、HTTPRoute Accepted/ResolvedRefs=True，HTTP 转发正确；实际 SecurityPolicy BasicAuth 无认证返回 401，正确认证返回后端内容。

- Loki + Alloy：唯一测试日志 `addon-e2e-log-proof-20261002` 经 Alloy 的 Kubernetes API 采集后，在 Loki `query_range` 中查询到。测试使用 Monolithic 模式（values 配置仍为 `singleBinary`）；NFS root_squash 下需要在服务器端赋予容器 UID/GID 10001 写权限，本轮仅调整 Loki 测试 PVC 子目录为 10001:10001、0770。
- Velero：真实 RustFS S3 put/get 通过；Backup 备份 17 个资源，PodVolumeBackup 完成 34 字节文件数据备份。随后删除源 ConfigMap、擦除原卷文件，Restore/PodVolumeRestore 完成，在新 namespace 中验证资源和文件内容一致。使用 AWS 插件 v1.14.0、Kopia 文件系统备份，不包含云盘快照。
- Kyverno：经典 Policy 实际允许合规资源、拒绝不合规资源，确认拒绝对象未落地。另用当前 `policies.kyverno.io/v1` 的 NamespacedValidatingPolicy 完成服务端准入允许/拒绝验证。该 API 的就绪字段是 `status.conditionStatus.ready`；初始测试错误等待 `status.conditions` 超时，修正断言后通过。两项测试策略均已删除。
- KEDA：真实 Prometheus 触发器使测试 Deployment 从 0 扩为 2 个 Ready 副本；信号变为 0 后，Deployment 与 Pod 数恢复到 0。
- Traefik：从集群外 lb2 请求 NodePort，正确 hostname/path 返回后端内容，错误 hostname 返回 404。TLS 请求使用 cert-manager 签发证书作为信任根，并校验 SNI/hostname，成功获取业务响应；未使用跳过证书校验。
- 菜单交互：真实 PTY 完成英文→中文→英文切换；中文“否”取消安装；非法菜单输入返回提示并可继续。
- 重复执行：逐一进入全部 14 个已安装插件，选择不重新配置/升级；全部正常返回菜单，16 个 release 的 revision 均未变化。验证脚本 SHA-256：`fcd7a7d492d82687b4350dd3dd63a25be6cd72ec3e50aa0750148a01c0f81411`，本机与 cp1 一致。

## 实际版本

| 插件 | Chart | 应用 |
|---|---|---|
| Metrics Server | 3.14.0 | 0.9.0 |
| cert-manager | v1.21.2 | v1.21.2 |
| kube-prometheus-stack | 91.8.2 | Prometheus Operator v0.94.1 |
| Loki / Alloy | 18.13.7 / 1.13.0 | 3.7.8 / v1.20.0 |
| Argo CD | 10.9.6 | v3.5.3 |
| External Secrets | 2.11.0 | v2.11.0 |
| ExternalDNS | 1.23.0 | 0.23.0 |
| Velero | 12.2.0 | 1.18.2 |
| NFS CSI | 4.13.4 | 4.13.4 |
| Envoy Gateway | v1.9.2 | v1.9.2 |
| Kyverno | 3.9.1 | v1.19.1 |
| MetalLB | 0.16.1 | v0.16.1 |
| KEDA | 2.21.0 | 2.21.0 |
| Traefik | 41.6.1 | v3.7.13 |

## 证据与复查

本轮原始 PTY 日志、交互记录、测试 values 和资源快照已随本地 `e2e/` 目录清理；仅保留本文中的历史验证结论、版本、功能断言与限制，无法再从本目录复查原始证据。部署脚本不依赖这些测试文件。删除本地记录不会卸载插件或清理集群、服务器上的测试资源。

在 cp1 上复查：

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
sudo -E helm list -A
sudo -E kubectl get nodes
sudo -E kubectl get pods -A
sudo -E kubectl top nodes
sudo -E kubectl get apiservice v1beta1.metrics.k8s.io v1beta1.external.metrics.k8s.io
sudo -E kubectl -n velero get backupstoragelocations,backups,restores,podvolumebackups,podvolumerestores
sudo -E kubectl -n addon-e2e get gateways,httproutes,services,externalsecrets,scaledobjects
sudo -E kubectl -n argocd get applications addon-e2e-gitops
```

## 保留资源和清理边界

- 已撤销：6 个节点的临时 containerd 代理 drop-in、测试 GitHub Pages hosts 固定、lb2 的代理转发服务和 Mac SSH 反向隧道。Docker Hub 的本轮试验镜像代理文件已删除；原有 registry.k8s.io 镜像代理保留。
- 已删除：测试强制准入策略、始终触发的测试告警规则、持续产生日志的测试 Pod。KEDA 测试 Deployment 已缩到 0。
- 为方便检查插件，保留安装的 release、测试 namespace 和真实测试依赖：lb2 的 NFS `/srv/addon-e2e-nfs`、DNS `addon-e2e-dns`（5353）、只读 Git `addon-e2e-git`（9418），以及 `addon-e2e/s3-test` 的 RustFS。临时 DNS/Git 是 systemd transient unit，重启后的自动恢复不在本轮范围；RustFS 使用 emptyDir，不能作为生产备份存储。
- 当前镜像已缓存到实际运行节点。新增节点、调度到未缓存节点或升级仍需可访问仓库；撤销测试代理不等于直连网络已修复。
- 测试使用 `addon-e2e-nfs` StorageClass，回收策略 Retain；删除 PVC 不自动删除服务器端数据。清理时只能处理确认属于本测试的 PV/子目录，不可直接删除 NFS 导出或其他数据。

## 验证范围

本次证明上述版本在当前 Kubernetes v1.36.5 测试集群的核心功能路径可用。没有验证所有插件功能、所有云厂商凭据、ACME 公网签发、生产高可用/负载容量、云盘快照或长期数据可靠性。监控组件使用低资源测试配置，部分数据采用临时存储。03 默认查询最新稳定 Chart，未来版本变化后应重新执行功能验证。
