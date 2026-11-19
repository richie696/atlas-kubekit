# 可销毁环境验收驱动

`monitoring_prometheus.py --confirm atlas-monitor-e2e` 启动临时 Prometheus 和既有测试 exporter，验证目标正常但样本过期、精确选择器新鲜、零匹配、多序列匹配、目标断开与恢复。测试同时核对样本通过后风险仍需通知证据，退出前清理专属容器和网络。

`monitoring_operator.py` 的基础场景同时核对逐风险规则来源。测试创建两条同组同名规则声明，只选中其中一条，确认另一条仍报告漏载。调整选择标签后，等待两条规则真实加载且求值健康，并清理专属规则和临时 Kind 集群。

`monitoring_systemd_remove.py` 使用已存在的本机 Ubuntu systemd 测试镜像。镜像可从 `ubuntu:24.04` 安装 `systemd systemd-sysv python3` 构建。驱动启动无网络、无主机目录挂载的临时容器，并通过真实主机代码包运行采集器安装、卸载和重装。

```bash
python3 tests/e2e/monitoring_systemd_remove.py \
  --image LOCAL_SYSTEMD_IMAGE --confirm atlas-monitor-e2e
```

2026-11-18 Ubuntu 24.04 systemd 测试通过。修改后的 unit 和符号链接均被拒绝，正常卸载保留其他 textfile 和测试用 kubelet/containerd 服务 PID，重复卸载与重装成功。临时容器和本次构建的镜像已清理。隔离集群的真实 SSH 和 06 计划执行证据另见[生产故障定位清单](../../docs/07-监控与预警/04-监控告警验证与生产故障定位清单.md)。

同日 `monitoring_operator.py --self-monitoring` 通过真实告警求值分别验证 kubelet 服务异常和 textfile 停更。模拟服务异常时持续刷新文件。模拟停更时让 exporter 持续读取过期文件，验证实际遥测告警触发，再刷新文件并确认告警解除。临时 Kind 集群已清理。

此目录是测试源码，CI 仅执行可移植回归，不持有服务器凭据，也不自动运行真实部署。
本机可分别运行 `monitoring_prometheus.py`、`monitoring_rules.py` 和
`monitoring_alertmanager.py`，参数均为 `--confirm atlas-monitor-e2e`。它们使用本机已有的
Docker 镜像和专属临时资源；分别验证真实 Prometheus 指标采集、active target 抓取池清单的正常/失败/恢复、规则求值、Alertmanager 到
受控接收端的故障及恢复消息。它们不验证 Kubernetes Operator、真实机器人或邮件送达。
`monitoring_etcd_quorum_live.py --confirm atlas-etcd-quorum-e2e` 只使用本机已缓存的
`registry.k8s.io/etcd:3.6.8-0`、`prom/prometheus:v3.14.0` 和 PyYAML；在唯一标签的临时 Docker 网络启动三成员 etcd，验证真实仲裁写入、丢失、恢复、数据保持，以及 Prometheus 多数派规则 firing/resolved。随后让宿主机上的独立观察器探测 Prometheus `/-/healthy`，停止/恢复 Prometheus 并验证观察状态切换。通知由测试内存接收回调确认，不连接真实机器人或邮件；容器共用单台 Docker 宿主机，不等于跨 VM/物理故障域测试。脚本仅在显式确认后运行，并核实删除自己创建的容器和网络。
`monitoring_alertmanager_grouping_live.py --confirm atlas-monitor-e2e-grouping --receiver-address <控制节点IPv4>`
需在可访问目标集群的控制节点上运行，并使用该节点的 admin kubeconfig。驱动创建带唯一标签的
临时 AlertmanagerConfig，接收器只绑定本机地址；验证实际 Alertmanager 的 cluster/alertname/team
分组、2 秒 group_wait、组内新增告警的 group_interval 更新及恢复通知；再创建一分钟静默，确认静默期间不投递、自动到期后 firing 通知恢复发送，并验证 resolved 通知。测试会删除临时静默、告警和配置，再停止接收器。
例如隔离实验室命令为 `sudo python3 tests/e2e/monitoring_alertmanager_grouping_live.py --confirm atlas-monitor-e2e-grouping --receiver-address 10.20.1.28`。
完整运行约需 70 秒以上；测试期间需允许 Alertmanager Pod 到控制节点接收端端口通信；不连接邮件或外部机器人。2026-11-17 在 Alertmanager 0.35.0 隔离集群完成实测，初次运行遇到临时路由未加载，三节点健康后重跑通过：三组告警正确分组，group_wait 为 2.00 秒，group_interval 更新为 1.8 秒，静默到期后 60.02 秒重新投递。测试退出前验证无活动测试告警和活动静默；测试告警和路由已清理，Alertmanager API 保留状态为 `expired` 的历史静默记录。
`monitoring_alert_delivery_live.py --kubeconfig /绝对路径/0600-kubeconfig --confirm atlas-monitor-notification-chain-e2e`
仅用于固定的 10.20.1.28–.30 隔离集群。驱动先核对 context、三节点身份与 Ready 状态、Prometheus/Alertmanager 选择器，
拒绝已有 AlertmanagerConfig 路由；随后在 w1 创建只接收集群内请求的临时 HTTP 接收器，发布唯一匹配路由和中文 PrometheusRule，
确认真实规则进入 firing 且 Alertmanager 投递包含中文处理说明的 payload，再将规则条件改为 false 并确认 resolved 通知到达。
测试结束清理规则、路由、接收器和端口转发并核验无残留，不连接外部邮箱或机器人；需使用权限为 0600 的隔离集群 kubeconfig 和可用 API 通道。
`monitoring_email.py --confirm atlas-monitor-e2e` 默认使用生产集群当前版本 Alertmanager 0.35.0 和临时 SMTP
测试服务；也可用 `--version` 指定其他受支持版本。该测试验证 465 端口隐式 TLS、证书校验、测试账号认证、中文故障/恢复邮件、Subject 与 HTML 样式标记，
并断言 MIME 正文仅包含 HTML；不会连接真实邮箱。0.35.0 已于 2026-11-16 通过此 E2E。
`monitoring_rules.py --confirm atlas-monitor-e2e` 使用固定版本 promtool 实际求值 Prometheus 规则，包含 etcd 三成员多数派保持、失去多数派及恢复；etcd 成员指标是受控样本，不替代真实 etcd 集群故障演练。

`monitoring_node_network.py --confirm atlas-monitor-e2e` 使用固定版 Promtool 验证 06 生成的网卡带宽、丢包、TCP 重传和监听丢弃告警。受控样本覆盖越界持续时间、恢复、计数器复位、低流量、陈旧和重复样本以及无有效链路容量。
`monitoring_node_resources.py --confirm atlas-monitor-e2e` 使用固定版本 promtool 验证每节点 CPU、内存、根文件系统和 inode 的可配置阈值触发/恢复，以及 CPU/内存指标缺失和 CPU 样本超出配置年龄但仍处于 Prometheus 默认查找窗口时的告警；CPU 样本按 5 分钟 `rate` 窗口生成。它验证 PromQL 规则语义，不代表真实节点压力演练或生产阈值校准。
`monitoring_node_systemd.py --confirm atlas-monitor-e2e` 使用固定版 promtool 验证 kubelet/containerd systemd 服务故障、指标缺失、持续时间和恢复，并断言中文摘要和处理建议。2026-11-16 在 10.20.1.28–.30 隔离集群通过 06 `monitoring-systemd-collection` 计划为 cp1/w1/w2 安装采集器，再将 03 的安全默认 values 合入现有 kube-prometheus-stack 93.3.0 release。验证 node-exporter DaemonSet 3/3 Ready、textfile hostPath 只读挂载、Prometheus 实际抓取三节点 kubelet/containerd 状态；短时停止 w2 kubelet 后观察到中文 `AtlasKubeletServiceUnavailable` firing，恢复后节点 Ready 且告警 inactive。此验证保留采集配置作为隔离测试集群基线；03 全新交互安装已另在干净的一节点 Kind 集群通过，但本次 kubelet 故障未验证通知外发。

2026-11-16 在干净的一节点 Kind 集群通过交互方式完整运行 `03-install-addons.sh --lang zh_CN` 并首次安装 kube-prometheus-stack 93.3.0。所有监控 Pod Ready，Prometheus API 显示 253 条健康规则、Atlas Alertmanager/systemd 规则已加载且 node-exporter 目标为 up；同时确认 kube-proxy ServiceMonitor 未创建、Grafana 使用 Recreate 策略。安装采用临时存储；此结果验证脚本首次安装路径及安全默认值，不替代多节点首次部署、业务阈值或外发通知验收。

2026-11-18 在全新三节点 Kind v1.36.1（1 control-plane、2 worker）再次交互运行 `03-install-addons.sh --lang zh_CN`，首次安装 kube-prometheus-stack 93.4.0（Prometheus v3.16.0）。8 个监控 Pod 全部 Ready；Prometheus API 加载 160 条健康告警规则，其中含 7 条 Atlas 规则；node-exporter 三节点目标 3/3 UP 且三节点系统指标均有样本。确认 kube-proxy ServiceMonitor 默认关闭、Grafana 使用 `Recreate`，Alertmanager 仅选择带 `atlas-kubekit.io/managed-by=monitoring` 标签的配置。总计 25 个目标中 22 个 UP；Kind 容器中的 Controller Manager、Scheduler、etcd 仅监听回环地址，三个对应抓取目标在此测试拓扑不可达，不能据此判断 kubeadm 生产拓扑。Chart 使用临时存储；集群、Kubernetes Secret、port-forward 与 kubeconfig 均已清理。仍需使用目标生产拓扑核验 CSI、持久卷和控制面目标。
`monitoring_control_plane_live.py --prometheus-url http://127.0.0.1:19090 --confirm atlas-control-plane-live-e2e-28-30` 只读查询保留的 10.20.1.28–10.20.1.30 隔离集群 Prometheus，校验 API Server、单成员 etcd、Scheduler 和 Controller Manager 目标新鲜度、实际指标样本及 kube-prometheus-stack 默认告警规则状态。该驱动不修改集群、不注入故障；通过只证明观测时刻的采集与规则加载健康，不证明多控制面覆盖、生产阈值、仲裁失效或故障恢复。目标地址固定为该隔离实验室，其他环境会拒绝使用此确认标记。
`monitoring_longhorn_disk.py --confirm atlas-monitor-e2e` 使用固定版本 promtool 验证 Longhorn 重复 Manager 指标聚合、磁盘用量阈值触发/恢复、磁盘未就绪恢复及整体指标缺失；这验证 PromQL 语义，不代表真实集群采集、磁盘可调度容量或硬件故障域。
`monitoring_longhorn_disk_live.py --kubeconfig /绝对路径/0600-kubeconfig --context atlas-e2e-lab --prometheus-url http://127.0.0.1:19090 --confirm atlas-longhorn-disk-live-e2e-28-30` 仅用于固定的 10.20.1.28–.30 隔离集群。它先核对三节点、Longhorn 节点、Prometheus 的新鲜 usage/capacity/ready 指标及受管磁盘规则，然后复制生产磁盘容量规则并使用当前真实样本派生临时阈值，确认每个 node/disk 告警触发和恢复；退出时删除唯一临时 PrometheusRule 并确认测试告警消失。测试不修改磁盘、Longhorn 调度、卷或数据，只证明实集群采集及规则求值，不证明真实容量耗尽、调度可用余量、硬件故障域或数据恢复。
`monitoring_pipeline.py --confirm atlas-monitor-e2e` 验证真实 Prometheus 节点规则、
Alertmanager、回环鉴权接收服务和受控机器人替身之间的触发、重试与恢复，并检查中文规则名、节点、风险编号、摘要和处理说明。它不连接
真实 Kubernetes 集群或外部机器人。
`monitoring_lb_exporter.py --confirm atlas-monitor-e2e` 使用独立容器运行 HAProxy 2.8，
通过容器网络实际请求受 CIDR 限制的 `/metrics` 并检查前后端指标，然后按资源标签清理容器和网络；
它不访问真实 LB 节点、验证真实防火墙/Prometheus Operator，或检查 Keepalived/VIP 漂移。
运行前需本机缓存 `haproxy:2.8-alpine` 和 `python:3.13-slim` 镜像。
`monitoring_lb_rules.py --confirm atlas-monitor-e2e` 使用固定版 promtool 验证 HAProxy 后端连接失败滚动阈值、持续排队和恢复语义；它不代表真实 LB 流量或生产阈值已验收。
`monitoring_lb_live.py --lab-profile retained-28-33 --known-hosts /绝对路径/已核验-known_hosts --confirm atlas-monitoring-lb-live-e2e-28-33`
只允许连接固定的隔离实验室 cp1 `.28`、lb1 `.32`、lb2 `.33`；或使用
`--lab-profile install-34-39 --confirm atlas-monitoring-lb-live-e2e-34-39` 连接新装验证集群 cp1 `.34`、lb1 `.37`、lb2 `.38`。
两个档案都通过严格 SSH 主机密钥校验，并使用各自固定 API VIP。
默认依次停止备用 lb2 的 HAProxy 验证真实 Prometheus 目标/后端指标缺失告警触发与恢复，再停止主 lb1 的 Keepalived
验证 API VIP 独占切换至 lb2、服务恢复后回切 lb1；另停止两侧 Keepalived 验证无主告警，临时添加/删除重复 VIP 验证双主告警，按两侧 Prometheus owner 样本确认后运行两轮切换验证漂移阈值及恢复。每次注入都会恢复服务/临时地址并核验 VIP/API；漂移告警要等 10 分钟观察窗口自然结束才算清理完成。可用
`--scenario metrics-loss|vip-failover|vip-no-owner|vip-multiple-owners|vip-drift` 单独运行。该驱动会短暂停止隔离 LB 服务，要求 SSH 账号可非交互执行 sudo；不发送通知，
不覆盖网络分区、生产环境或业务流量容量。主机密钥必须通过可信渠道预先核验，禁止用未核验的
`ssh-keyscan` 输出作为信任依据。2026-11-16 隔离集群实测：lb2 exporter 停止触发目标与后端监控告警，恢复后告警解除；停止 lb1
Keepalived 后 VIP 独占迁移至 lb2，API `/readyz` 持续健康，恢复后 VIP 独占回到 lb1；无主和双主告警触发/恢复已通过；两轮 Prometheus 已采样的主备变化触发漂移告警，10 分钟窗口结束后告警解除。测试结束确认服务 active、VIP 仅在 lb1、API `/readyz` 为 `ok`。

2026-11-17 再次运行上述 `--scenario all` 完整套件，五个场景（HAProxy 指标缺失、VIP 正常切换、VIP 无主、VIP 双主、Keepalived 漂移）全部通过。收尾复核两台 LB 的 HAProxy、Keepalived 和 VIP exporter 均为 active，VIP 仅由 lb1 持有，API `/readyz` 为 `ok`，四个 Prometheus 指标目标均为 `up`，没有遗留 Atlas LB 告警。

2026-11-19 在干净安装验证环境再次以 `--lab-profile install-34-39 --scenario all` 完整通过五个场景。结束时 lb1/lb2 的 HAProxy、Keepalived 均为 active，VIP `.39` 仅在 lb1，API `/readyz` 为 `ok`；Prometheus 的两个 HAProxy 与两个 VIP exporter 目标均为 `up`，五类注入告警已恢复。
`tests/test_monitoring_observer_dns.py` 使用本机 UDP DNS 服务实际收发查询，验证预期 VIP、
错误地址、NXDOMAIN、畸形响应以及失败/恢复事件进入持久通知队列；它不访问真实 DNS 服务，
也不代替独立机器上的通知送达验收。
`monitoring_observer_systemd.py --confirm atlas-monitor-e2e` 构建仅供本次测试使用的 Ubuntu 24.04 镜像，
通过 06 号脚本的正式 CLI 安装/卸载观察者用户服务，并通过状态命令核验本机健康 HTTP 探测、私有 SQLite 状态及卸载后状态保留，
并通过 systemd watchdog 冻结观察进程，核验独立 `OnFailure` 单元经 WeCom Adapter 向本地 HTTPS 协议端点发送中文故障通知、主服务自动重启及恢复探测；
确认正常卸载不发送故障消息，最后删除临时容器、镜像和其中的测试凭据。需要 Docker、`ubuntu:24.04` 和构建阶段的 Ubuntu 软件源访问；
本地 HTTPS 端点只证明协议和模板路径，不证明企微平台送达，也不代替独立 Linux 主机、集群失联或共同故障域的生产验收。
2026-11-17 在隔离 LB2（10.20.1.33）以正式 06 号脚本
完成真实 API VIP `/readyz` 安装、跨 SSH 会话持续健康探测、状态查询及安全卸载；安装自动启用用户 linger。
同日在 LB1（10.20.1.32）经 06 正式入口安装临时观察器，双侧停止 Keepalived 使 API VIP 无主，观察器记录故障并由企微平台接受中文告警；恢复 LB2 后观察器记录恢复并由平台接受恢复通知，再启动 LB1 验证 VIP 回切和 API 健康。两个实机测试结束都移除临时服务、状态、凭据并恢复原 `linger=no`。这些结果证明观察器的安装生命周期、TLS 探测、API VIP 故障/恢复及通知调用路径；不证明 LB 与集群处于独立物理故障域。2026-11-17 同时在隔离 Ubuntu 24.04/systemd 容器冻结观察器进程，实测 watchdog 判定超时、`OnFailure` oneshot 通过本地 HTTPS WeCom 协议端点接受中文故障消息、systemd 重启主服务，并确认卸载不误发故障消息；这不证明真实企微平台收到此类进程故障通知。
`monitoring_kubernetes.py --kind /绝对路径/kind --confirm atlas-monitor-e2e` 使用本机
缓存的 kind 节点镜像、临时 kubeconfig 和临时单节点集群，验证规则受控计划、集群锁、
Kubernetes API 写入及资源漂移拒绝，结束后删除本次创建的集群。测试 CRD 为最小模拟，
不包含 Prometheus Operator，也不验证主机 SSH 或实际规则选中。
`monitoring_operator.py --kind /绝对路径/kind --confirm atlas-monitor-e2e` 在单独的临时
kind 集群安装固定版本 kube-prometheus-stack，并验证真实 Operator 选中规则及 Prometheus
加载并触发规则；同时将真实 PrometheusRule 清单和 `/api/v1/rules` 交给 63 项风险覆盖评估，
断言规则已加载时仍把数据和送达留为未验证；需要网络和额外容器资源。测试使用受控计划与真实集群锁，但主机身份核验
使用模拟环境；不验证完整生产部署、通知渠道或多节点故障。
基础运行还会经 06 CLI 对 `monitoring-scrape-rules` 执行预览、错误确认拒绝、确认应用、状态查询与幂等重放，
并从真实 API 检查计划写入的受管 PrometheusRule。仅清单/节点地址匹配由隔离测试环境适配器替代；CLI 参数解析、
计划文件、确认门禁、集群锁与日志、Kubernetes 写入、状态报告及 Operator 选中规则均实际执行。
该驱动还在真实 API 上注入 apply 时同名外部 PrometheusRule 冲突，确认旧计划失败并记录 `failed` 状态、保留锁；删除测试冲突后按 owner/resourceVersion 显式恢复锁，通过 `change-continue` 基于最新状态创建新 ID 计划，成功应用并核验其幂等重放。测试证明的是受控所有权冲突的失败/续作路径，不代表所有 capability 的远程主机部分都能自动续作。
同时会在同一临时集群制造带所有权标签的活跃锁，运行 `lock-status` 与精确 resourceVersion 确认的
`lock-recover`，并验证锁 owner 已清除。

监控资源安全移除验收沿用 06 变更计划。测试矩阵覆盖：仅允许 namespaced 的 Prometheus Operator
监控对象和 NetworkPolicy；对象必须同时带 Atlas monitoring 所有权标签及与当前 spec 一致的摘要；
预览后对象身份/resourceVersion 变化时 apply 必须拒绝且保留对象；错误确认不得删除；正确确认通过
Kubernetes DeleteOptions 的 UID/resourceVersion 前置条件删除；状态/计划重放不得再次产生删除请求；
缺少所有权或摘要不匹配的对象不得生成可执行计划。真实 API E2E 只对本测试创建的唯一 PrometheusRule
执行删除，不触碰集群内预先存在的资源。

2026-11-18 `monitoring_operator.py` 实际通过以上 06 CLI 移除路径：确认错误时规则仍在，正确确认后 API 查无对象，计划重放报告“先前已完成，本次未重复执行”；更改对象标签后旧计划拒绝执行并保留对象；无 Atlas 所有权标记的规则无法生成计划且保持存在。另在 CLI 读取计划后、DELETE 写入前并发修改对象，真实 API 因 resourceVersion 不匹配拒绝删除；失败 journal 与锁保留，按精确 owner/resourceVersion 恢复锁后完成测试资源清理，对象在冲突期间始终保留。该测试不覆盖五种白名单类型逐一的 API 行为，也不证明主机侧采集器或通知路由有安全回退。
附加 `--longhorn-collection` 在临时集群创建受控指标服务，通过 06 的变更计划写入
ServiceMonitor 和 NetworkPolicy，验证真实 Prometheus Operator 选中并抓取该服务；
kind 默认网络不验证 NetworkPolicy 实际拦截，也不代表真实 Longhorn 采集已接入安装流程。
`monitoring_network_policy.py --kubeconfig /绝对路径/kind.kubeconfig --context kind-atlas-netpol-e2e-名称 --confirm atlas-netpol-e2e`
要求目标为至少两节点、Cilium 已就绪的专用 kind 集群。它跨节点执行一次授权抓取和一次未授权连接，
分别断言成功与拦截；结束时删除并确认临时命名空间不存在。该驱动不创建或删除 kind 集群，
调用者须在验收后删除本次专用集群；不得指向生产 context。
`monitoring_longhorn_live_netpol.py --kubeconfig /绝对路径/kubeconfig --context atlas-e2e-lab \
--probe-image m.daocloud.io/docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662 \
--confirm atlas-longhorn-live-netpol-e2e-28-30`
只允许访问已登记的 10.20.1.28–10.20.1.30 隔离集群（cp1/w1/w2 与固定 IP 必须完全匹配），并要求 kubeconfig 权限为 0600。
执行前核对 Cilium、Prometheus 选择器、受管 Longhorn ServiceMonitor/NetworkPolicy、longhorn-backend 服务和 Ready 的 Manager。
它在 monitoring 命名空间创建带 Prometheus 匹配标签的临时 Job，从 w1 跨节点读取 w2 上真实 Manager 的
`longhorn_volume_robustness` 指标，再在临时非授权命名空间验证访问被拦截；结束后删除 Job 和命名空间并核实清理。2026-11-15 在 `atlas-e2e-lab` 实际运行通过，授权跨节点抓取成功、未授权来源被策略阻断，临时 Job 和命名空间均已删除。
该驱动不修改 Longhorn 或监控配置，不停止节点，不创建 PVC，也不得改为生产 context。运行前需在各节点缓存指定探测镜像。2026-11-18 在保留的 `atlas-e2e-lab`（10.20.1.28–.30）重跑通过：w1 的授权监控形态 Job 成功抓取 w2 Manager 的真实指标，无关命名空间被策略阻断；临时 Job 和命名空间均已删除并核实不存在，节点及 Longhorn Manager 保持健康。
`monitoring_cilium_live.py --prometheus-url http://127.0.0.1:19090 --confirm atlas-cilium-live-e2e-28-30` 在已按 06 计划/确认流程接入采集的保留 10.20.1.28–10.20.1.30 集群上只读检查 Prometheus：三个固定节点的 Cilium Agent 与 Operator 目标都须成功抓取，近期 Agent/Operator、IPAM 容量/使用率、eBPF map 压力和节点连通性样本存在，六条受管规则已加载且健康、当前未触发。该验证没有注入故障，不证明 IPAM/eBPF 阈值触发和恢复、Cilium 数据面丢包、Hubble 流量指标或告警送达；HTTP 仅允许回环地址，远程环境应使用证书校验正常的 HTTPS。
`monitoring_cilium_failure.py --kubeconfig /绝对路径/kubeconfig --context atlas-e2e-lab --prometheus-url http://127.0.0.1:19090 --confirm atlas-cilium-failure-e2e-28-30` 在 10.20.1.28–.30 保留集群中创建带 E2E 所有权标签的临时规则副本，将 IPAM/eBPF 阈值临时调低，确认两个告警均触发且含节点标签；随后将阈值调到 100%，确认告警解除，并删除临时 PrometheusRule。运行前要求固定节点/IP、Prometheus 选择器正确且集群中没有 AlertmanagerConfig 路由；它验证真实 Prometheus 规则求值，不注入真实 IPAM 耗尽或网络故障，也不验证渠道送达。清理只删除带本 E2E 标签的唯一命名对象。
`monitoring_workload_health_live.py --kubeconfig /绝对路径/kubeconfig --context atlas-e2e-lab \
--prometheus-url http://127.0.0.1:19090 --confirm atlas-monitor-e2e` 只允许访问保留的 10.20.1.28–.30 隔离集群，要求私有 kubeconfig、预先安装并被 Prometheus 选择的受管工作负载规则，以及没有 AlertmanagerConfig 路由。它在唯一临时命名空间创建长运行 Job、未暂停 CronJob 和使用不存在节点标签的 Pending Pod，验证真实 kube-state-metrics 提供 Job 活动/开始时间、CronJob 下一计划/暂停状态指标，并验证 Pending 告警触发；随后删除 namespace 并确认告警恢复。使用 06 计划中的生产默认阈值，不等待 Job 超时或 CronJob 漏跑，也不访问业务服务或发送外部通知。
`python3 tests/e2e/monitoring_workload_failure_live.py --kubeconfig /绝对路径/kubeconfig --context atlas-e2e-lab \
--prometheus-url http://127.0.0.1:19090 --confirm atlas-monitor-e2e` 只允许访问固定的 10.20.1.28–.30 隔离三节点集群。它使用保留域名 `.invalid` 的不可达 registry 触发真实 `ImagePullBackOff`，再使用缓存的 BusyBox 镜像触发 CrashLoop 重启突增和内存限制 OOMKilled；检查 Kubernetes 拉取失败/BackOff/终止状态、Prometheus 中文告警标签及删除 namespace 后的恢复，结束时清理测试 namespace。它不关闭节点，不测试节点级 OOM/驱逐或外部通知送达。
`sudo python3 tests/e2e/monitoring_workload_timeouts_live.py --kubeconfig /绝对路径/0600-kubeconfig --confirm atlas-monitor-e2e-timeout` 必须在 cp1 上以 root 执行，且只接受 10.20.1.28–.30 固定三节点集群。它从已发布的受管 PrometheusRule 派生 60 秒临时测试阈值，验证真实 Job 超时及 CronJob 漏跑告警；CronJob 用例短暂停止唯一 kube-controller-manager，并由独立看门进程在 240 秒内自动恢复静态 Pod 清单及清理测试资源。测试会恢复控制器、删除临时 PrometheusRule 和 namespace，并等待告警清除、节点 Ready。该操作会短暂暂停控制器协调，只能用于隔离测试集群；脚本不验证默认 Job 时限或业务任务成功 SLO。

固定实验室专项暂不能在本机复核时，可在销毁型 Kind 集群运行同样的真实 Job/CronJob 告警链路。命令只对本次自动创建且通过 Docker Kind 标签和节点身份校验的单节点控制面容器移动静态 Pod 清单；240 秒独立看门进程负责恢复。自动恢复后检查控制面和节点 Ready，再移除测试规则、namespace 并确认告警清空：

```bash
python3 tests/e2e/monitoring_operator.py \
  --kind /绝对路径/kind \
  --confirm atlas-monitor-e2e \
  --workload-timeouts \
  --monitoring-chart /绝对路径/kube-prometheus-stack-92.22.0.tgz
```

该模式自动启用 KEDA 固定版本以加载相同的受管工作负载规则；这里的 KEDA 指标 E2E 与 timeout 子场景分别验证。Kind 控制器暂停只发生于本次 disposable 集群，不影响现有三节点实验室或其他 Kind 集群。

2026-11-18 在 Kind v1.36.1 实际运行通过：Job 超时和 CronJob 漏跑规则均真实 firing，摘要为中文；CronJob 注入期间 Controller Manager 的静态清单移出 manifests 目录以避免 kubelet 将备份识别为另一份 Pod 定义，恢复后 Controller Manager 和节点 Ready。测试脚本确认临时告警、规则、namespace、Kind 集群及 kubeconfig 清理完成。测试使用 60 秒临时阈值，不代表默认生产阈值或作业 SLO。
`python3 tests/e2e/monitoring_container_start_failure_live.py --kubeconfig /绝对路径/kubeconfig --context atlas-e2e-lab \
--prometheus-url http://127.0.0.1:19090 --confirm atlas-monitor-e2e` 也只允许访问该隔离集群。它通过引用不存在的 Secret 产生真实 `CreateContainerConfigError`，检查 kube-state-metrics 原因标签、`AtlasPodContainerStartFailure` 告警触发，并在删除 Pod 和临时 namespace 后确认恢复。该用例不覆盖挂载失败或事件正文关联。

R-WORK-04 的 Deployment 规则测试覆盖 `ProgressDeadlineExceeded` 触发与状态清除恢复，并确认其他状态、过期指标不产生发布卡滞告警。隔离 kube-state-metrics Operator E2E 使用唯一 Deployment、短 `progressDeadlineSeconds` 和不可拉取镜像触发真实发布失败；验证期望/可用副本告警和发布卡滞告警都进入 firing，恢复正确镜像后两个告警都解除，最后清理测试 Deployment。该用例已通过，临时 kind 集群已删除。测试只运行于可销毁 kind 集群。
cert-manager 证书规则只面向显式登记的 `namespace/name`，测试覆盖有效期阈值内/外、Ready=False 与恢复、到期指标及就绪指标同时缺失/陈旧时的告警边界。Promtool 和 06 计划发布单测已通过。2026-11-15 在保留三节点集群通过 03 交互菜单临时安装 cert-manager 1.21.3，脚本自动创建 PodMonitor，Prometheus 实际抓取 controller、cainjector、webhook 三个目标；真实签发、到期/未就绪/缺失告警与恢复也已验证。测试结束后卸载控制器、PodMonitor、CRD 和临时证书，三节点均 Ready。
附加 `--email` 验证通过 06 变更计划发布的邮件路由被真实 Operator 选中，并检查 Operator 最终生成配置包含中文 HTML 模板及卡片样式，且不包含纯文本正文；不验证该路由的 SMTP 实际送达。隔离 SMTP E2E 验证实际邮件只包含内联样式 HTML；目标集群当前邮件的 HTML 展示仍需查看活动 AlertmanagerConfig 并在该集群发送新测试邮件验收。

R-WORK-05 扩展的规则测试覆盖四个边界：活动 Job 超过配置时限后告警、时限内不告警、超时后恢复，以及活跃 CronJob 的下一次计划长期逾期后告警并恢复；暂停 CronJob 不应产生漏跑告警，缺失或过期的 kube-state-metrics 样本不得触发。阈值通过 06 变更计划配置，默认 Job 超时 3600 秒、CronJob 逾期 900 秒。该组规则依赖 kube-state-metrics 的 `kube_job_status_active`、`kube_job_status_start_time`、`kube_cronjob_next_schedule_time` 和 `kube_cronjob_spec_suspend` 指标；promtool 覆盖 PromQL 边界，真实作业的业务时限仍须由集群管理员按作业性质调整。

CoreDNS 副本规则使用显式登记的 Deployment 和最低可用数。Promtool 验证缺少 kube-state-metrics 样本时告警、多个 KSM 抓取目标正确折叠、低于最低可用数触发并在恢复后解除；Operator E2E 始终启用真实 kube-state-metrics 作为 CoreDNS 健康基线，附加 `--workload` 时还会让专用 Deployment 从可用变为不可用再恢复。
附加 `--dns-failure` 在一次性 Kind 集群为实际 CoreDNS 配置专用测试域。用测试 Pod 验证成功应答、SERVFAIL 和延迟应答，对照 Prometheus 告警及正文中的错误比例和秒数。测试只切断指标 Service 的端点后复核 DNS 正常、采集缺失告警触发及恢复，然后还原 Corefile 并清理集群。使用 `python3 tests/e2e/monitoring_operator.py --kind /绝对路径/kind --confirm atlas-monitor-e2e --dns-failure`，如需本地 Chart，附加 `--monitoring-chart /绝对路径/kube-prometheus-stack-92.22.0.tgz`。

附加 `--robot` 验证 Alertmanager Operator 选中受管认证机器人路由，将 Secret 引用解析到 Operator 生成的 Alertmanager Kubernetes Secret，并通过 `monitoring-relay-metrics` 变更计划发布 PodMonitor/PrometheusRule。受控 Pod 使用 Bearer token 提供模拟 relay 计数；测试检查 Prometheus 实际抓取、告警触发和计数窗口过期后的恢复，不部署产品 Relay、不模拟真实渠道投递，也不发送群消息。
可在同一临时集群中组合验证邮件、机器人、LB、Longhorn 采集和工作负载控制器：
`python3 tests/e2e/monitoring_operator.py --kind /绝对路径/kind --confirm atlas-monitor-e2e --workload --controllers --longhorn-collection --email --robot --lb`。

2026-11-16 以上述组合参数再加 `--self-monitoring` 的完整运行通过：邮件 HTML-only 配置、机器人 Secret 引用、HAProxy 和 Longhorn 测试指标采集、通知中继鉴权采集及故障告警恢复均通过；Deployment、StatefulSet、DaemonSet 告警均验证健康→故障→恢复。测试使用临时模拟目标和假凭据，不发送外部通知，也不验证真实 Longhorn 故障或 NetworkPolicy 强制执行；Kind 集群已自动删除并确认不存在。

`--email` 在一次性 Kind 集群应用正式邮件配置计划，检查计划只保存引用，并验证 Operator 轮换凭据。凭据清理用例检查同接收器的明确候选、Pod 与 ServiceMonitor 引用、其他会话暂存凭据、共享中继令牌和版本变化。仍有引用的候选会保留，删除最后一个引用后复核其可被安全清理。该测试使用占位 SMTP 配置，结束后删除本次 Kind 集群。
组合验证覆盖 cert-manager 规则选择与缺失指标告警、邮件 HTML-only 配置、机器人 Secret 引用、风险报告、HAProxy 告警、受管 ServiceMonitor 抓取，以及 Deployment 发布卡滞/恢复和 Deployment/StatefulSet/DaemonSet 故障与恢复。该组合不发送真实通知，也不包含 Longhorn 故障注入；真实邮箱/机器人送达由独立渠道 E2E 与用户收件确认，真实 cert-manager 和 Longhorn 故障恢复由各自的隔离集群 E2E 验证。
附加 `--lb` 会在隔离集群运行真实 HAProxy exporter，经受管 Endpoints/ServiceMonitor 抓取实际指标，
再验证后端不可用规则触发且 Prometheus 告警实例带有中文摘要和处理建议。它不验证真实 LB 主机、
生产网络防火墙或 VIP 漂移。邮件模板的中文内容与 SMTP 投递由 `monitoring_email.py` 独立验证；
`--email --lb` 可同时确认 Operator 选中的邮件路由与 HAProxy 告警规则，但不会连接真实邮箱。
运行前需在本机缓存 `kindest/node:v1.36.1`、`prom/prometheus:v3.14.0` 和 Operator、
config-reloader 的 `v0.94.2` 镜像；附加 `--lb` 还需 `haproxy:2.8-alpine`。
测试使用镜像导入避免依赖集群节点的代理设置。
附加 `--keda` 会安装固定版本 KEDA 2.21.0，配置 Operator metrics 和带 `release=stack`
标签的 ServiceMonitor；验证 Prometheus 实际抓取、坏 Prometheus scaler 触发中文错误告警，
切换到有效测试查询后等待告警恢复，并删除/恢复 Operator ServiceMonitor 验证目标失联告警触发/解除。
该目标缺失规则等待 `monitoring-max-age` 窗口到期后告警。需本机缓存三个 `ghcr.io/kedacore/*:2.21.0` 镜像，
并能访问 KEDA Helm Chart 仓库。该测试使用隔离 kind 集群和无业务数据的临时 ScaledObject。
附加 `--self-monitoring` 会将 03 的 `monitoring_values.py --safe-defaults` 原样合入固定 Chart 安装，检查 kube-prometheus-stack 默认规则、Alertmanager、node-exporter textfile 参数/只读 hostPath 和自监控规则；确认 Prometheus 实际抓取 CPU/内存/文件系统/inode 及测试节点写入的 Atlas systemd textfile 样本，并验证 kubelet systemd 告警触发/恢复。该模式也断言 CSI 未附加及状态遥测缺失两条规则已由 Prometheus 加载且健康；2026-11-18 新建 Kind v1.36.1 隔离集群通过该检查。另通过 06 目标规则监控 Alertmanager endpoint，删除/恢复 ServiceMonitor 验证目标缺失告警触发/解除。需本机缓存 `prom/node-exporter:v1.9.1`。systemd 指标由测试夹具写入，不代表真实节点 timer 安装；单独使用此模式时 CSI 指标只验证规则加载，不创建真实 VolumeAttachment；此模式不停止监控组件进程，也不证明节点资源告警阈值适用。
附加 `--tsdb-capacity --csi-source /绝对路径/CSI源码` 会使用独立的两节点 kind 集群、固定版本 CSI hostpath 测试驱动和持久化 Prometheus TSDB PVC，验证 kubelet 实际容量指标、06 PVC 告警的阈值触发与恢复；不会填满卷，也不代表 Longhorn 实际磁盘耗尽或 WAL 增长测试。需要预先缓存该测试所需 CSI 镜像及 PyYAML。
Operator 集成测试始终需要缓存 `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0`。
附加 `--workload` 时还需缓存 `python:3.13-slim`；测试在临时集群里让专用 Deployment 从可用变为不可用再恢复。
附加 `--controllers` 会验证 StatefulSet 和 DaemonSet 的故障及恢复；StatefulSet 回退后
仅删除测试 Pod 以解除滚动更新阻塞。
附加 `--storage` 时还需缓存 `python:3.13-slim`；测试挂载临时 hostPath PVC 并读取真实
kubelet 指标。若指标可用，则通过改变告警阈值验证触发与恢复；若指标不可用，则验证
缺失告警并报告低空间测试未完成，命令以非零状态退出以免把部分验证报告为通过。hostPath 路径不保证提供容量统计。
使用 `--csi-source /绝对路径/CSI源码` 改用两节点 kind 和固定版本的上游 CSI hostpath
测试驱动；需预先缓存测试所列的 CSI 镜像并提供 Python PyYAML。配合 `--storage` 已验证临时 CSI PVC 的真实 kubelet 容量指标、阈值告警触发与恢复、CSI 插件重启、卷重挂载和测试 worker 停启后的数据保持；配合 `--tsdb-capacity` 则验证 Prometheus 实际 TSDB PVC 的容量规则触发与恢复。配合 `--csi-attachment-failure` 会启用测试驱动的 attach 功能和 external-attacher，在测试 PVC 预先绑定后停止 CSI 控制器，验证真实 VolumeAttachment `attached=false`、KSM 附加指标为 0、`R-STOR-02` 中文告警触发；恢复控制器后验证附加指标回到 1、Pod 挂载 Ready、告警解除。该模式还需缓存 `registry.k8s.io/sig-storage/csi-attacher:v4.13.0`。执行示例：

```bash
python3 tests/e2e/monitoring_operator.py \
  --kind /绝对路径/kind \
  --confirm atlas-monitor-e2e \
  --csi-source /绝对路径/CSI源码 \
  --csi-attachment-failure \
  --monitoring-chart /绝对路径/kube-prometheus-stack-92.22.0.tgz
```

2026-11-18 在隔离 Kind v1.36.1 通过该场景；测试使用固定版本的 hostpath 样例驱动，不代表 Longhorn/生产 CSI 驱动行为。
此驱动是测试用 hostpath 实现，不代表生产 CSI，也不能证明 Longhorn 多副本或跨节点卷故障切换。网络受限时可用 `--monitoring-chart /绝对路径/kube-prometheus-stack-92.22.0.tgz` 指向本地 Chart 归档，避免 Helm 仓库访问；测试会读取归档中的 `Chart.yaml` 并拒绝名称或版本不是 `kube-prometheus-stack 92.22.0` 的文件。没有该参数时从 Helm 仓库安装同一固定版本。
真实 E2E 需要维护者已获授权的独立 VM、可信 SSH 主机公钥和专用私密输出目录。
一次只运行一套完整测试集群；上一批完成后及时关闭或删除其自有 VM，再启动下一批。
保留模板、物料或网关时明确用途，用完即释放。不要根据 IP 扫描或名称前缀删除现有集群。

先核对 Proxmox VM ID、标签和实例归属，再通过控制台/QGA 在每台独立克隆写入
root 拥有的 `/etc/atlas-kubekit-e2e-lab.json`，内容为：

```json
{"schemaVersion":1,"labId":"atlas-e2e-offline","node":"lb1","vmid":221}
```

每台的 `node`、`vmid` 必须与私密 manifest 一致；这个标记是操作前提，不能由驱动
在待测机器上自行补写来绕过归属检查。离线节点使用隔离的内网网关或明确的 Service 网段路由，
网关必须阻断外网转发；无互联网与无基础路由是两个条件。使用独立地址段和 VRID，
禁止指向现有集群。管理机的 `lab.json` 必须属于当前用户且权限为 0600，例如：

```json
{
  "schemaVersion":1,"labId":"atlas-e2e-offline","network":"172.29.230.0/24","offlineGateway":"172.29.230.1",
  "sshConfig":"/私密目录/ssh_config","adminPublicKey":"/用户目录/.ssh/id_ed25519.pub",
  "materialId":"通过可信渠道核对的64位物料ID",
  "nodes":[
    {"name":"lb1","address":"172.29.230.31","vmid":221,"interface":"enp6s18"},
    {"name":"lb2","address":"172.29.230.32","vmid":222,"interface":"enp6s18"},
    {"name":"cp1","address":"172.29.230.33","vmid":223,"interface":"enp6s18"}
  ]
}
```

按 [离线交付](../../docs/06-离线交付/01-锁定物料与离线部署.md) 将**同一物料包**分发至全部节点并激活。
校验 `tools/atlas-kubekit.tar.gz` 的锁定 SHA 后，将其中源码解压至每台
`$HOME/atlas030-e2e-source`，不要覆盖为另一份工作区源码；将符合版本/身份/VIP 的统一
集群配置放到首台该目录的 `lab-cluster.json`。驱动使用临时 ubuntu 运维通道；安全收尾另测。

在项目根目录执行：

```bash
python3 tests/e2e/run.py --lab /私密目录/lab.json --confirm atlas-e2e-offline \
  --scenario phase-one --node lb1 --output /私密目录/phase-one-lb1
# 对其余节点逐一执行；每台重启后通过 QGA 核对新主机公钥，更新可信 SSH 文件。
# 核对 01 ready、唯一身份、登记的内网网关及外网转发阻断规则。
python3 tests/e2e/run.py --lab /私密目录/lab.json --confirm atlas-e2e-offline \
  --scenario deploy --output /私密目录/first-deploy
python3 tests/e2e/run.py --lab /私密目录/lab.json --confirm atlas-e2e-offline \
  --scenario repeat --output /私密目录/repeat-deploy
```

部署用例核对标记、hostname、01 ready 与全部节点物料 ID，执行真实配对和原生 02。
重复用例应进一步比较配置 SHA、服务 PID、Pod UID 和 Helm revision，证明只读重跑边界。
每个场景独立目录，日志 0600，报告保留失败；配对码只在内存中向隐藏输入提交。
输出目录不可提交。退出 255 仅表示 01 重启中断，报告明确要求后续验证，不能视作 01 已验收。

这些驱动不声称覆盖所有生命周期/灾备场景。真实的 PDB、故障注入、锁恢复、企业仓库、
应用保护和 etcd 恢复应分别留存独立结果和清理证明；不能从该驱动退出 0 推断全版本验收通过。

monitoring_longhorn_failure.py 是真实集群副本故障演练，需使用只供测试的 context（名称须包含 e2e、lab 或 test）、权限为 0600 的 kubeconfig、两台可调度 Longhorn 节点、已选择受管 PrometheusRule 的 Prometheus，以及本机 loopback HTTP port-forward 或证书校验正常的 HTTPS Prometheus 地址。驱动为临时 PVC 创建两副本卷，要求副本分别落在指定的故障节点和幸存节点，然后仅隔离该节点的 Longhorn 调度并删除该测试卷的副本对象；它不会关闭整台 VM 或 kubelet。演练期间检查 Prometheus 健康/降级指标和告警，再等待副本重建、告警恢复并核对真实 payload SHA256。退出时恢复节点原有调度设置，并删除该运行创建的 namespace、StorageClass 和 PrometheusRule；如果清理未完成会以失败退出。2026-11-15 在隔离三节点集群 `atlas-e2e-lab` 的 w1/w2 上实跑通过：副本降级告警触发、恢复至两份运行副本、告警解除、哨兵 SHA256 一致；节点调度状态恢复，所有临时对象及 Longhorn 卷均已核实删除。运行前确保 context 是专用隔离环境，Prometheus 使用的 release 标签与当前集群匹配。

2026-11-19 在新装隔离集群 `.34–.36` 重跑：`w2` 上演练卷的一个副本被移除后，真实 Prometheus 检测到 degraded 并触发告警；`w1` 上写入/读取的哨兵 SHA256 为 `bcf87373248db555c8254170f24d47caa7b690da1d398979fa5566212269c94f`，故障及恢复后保持一致；重新允许调度后两副本恢复运行且告警解除。驱动核验临时 Namespace、StorageClass、PrometheusRule、Longhorn 卷删除和原节点调度状态恢复。

新装隔离环境可按如下方式运行。先在本机启动 Prometheus loopback port-forward，再使用权限为 0600 的专用 kubeconfig；参数中的 context 和节点名须与目标集群实际值一致：

```bash
kubectl --kubeconfig /绝对路径/0600-kubeconfig --context atlas-e2e-install-validation \
  -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090
python3 tests/e2e/monitoring_longhorn_failure.py \
  --kubeconfig /绝对路径/0600-kubeconfig \
  --expected-context atlas-e2e-install-validation \
  --failed-replica-node w2 --survivor-node w1 \
  --prometheus-url http://127.0.0.1:19090 \
  --prometheus-release kube-prometheus-stack \
  --confirm atlas-longhorn-replica-failure-e2e
```

### 日志链路自监控

运行 `python3 tests/e2e/monitoring_logging_rules.py --confirm atlas-monitor-e2e` 验证默认规则时长、采集缺失和丢弃告警边界。运行 `python3 tests/e2e/monitoring_logging_pipeline.py --confirm atlas-monitor-e2e`，在一次性 Docker 网络中启动 Loki 3.7.7、Alloy 1.22.0 和 Prometheus 3.5.0。超长日志实验应触发 Loki `line_too_long` 拒收与 Alloy 投递丢弃，恢复正常写入后告警应解除。运行前本地需缓存这三个镜像。
