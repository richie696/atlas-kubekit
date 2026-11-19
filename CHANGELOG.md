# Atlas KubeKit — 变更记录

本文件记录面向使用者的功能、兼容性与已知限制。工具集版本以根目录 [VERSION](VERSION) 为准。

## 0.4.0 — 候选发布

0.4.0 监控与告警功能已完成开发和候选发布验收。按部署用途区分 Kubernetes 集群：监控集群运行完整监控中心，应用集群按需接入采集代理；监控组件仍由 Kubernetes 管理，不新增宿主机 Docker Compose 部署入口。候选版本全量回归 572 项通过、1 项受环境限制跳过，`git diff --check` 通过。尚未创建正式提交、tag 或 GitHub Release。

- 截至 2026-11-19 01:26，在隔离安装集群 `.34–.39` 完成候选验收：HAProxy 遥测丢失、VIP 主备切换、无主、双主和漂移告警均按预期触发并恢复；最终 32/32 个 Prometheus 目标正常、没有 Atlas firing 告警，`.39` VIP 由 lb1 持有，API `/readyz` 正常。真实 Longhorn CSI 卷在移除 `w2` 上一个副本后触发降级告警；`w1` 持续读取哨兵数据且 SHA256 不变，重新启用调度后两副本恢复、告警解除、校验和一致。隔离 lb1 上的独立 systemd 观察器检测到 VIP 无主，并记录企微平台接受的故障/恢复通知；观察器保留在 lb1 供后续实验室测试。临时 Namespace、StorageClass、PrometheusRule、PVC/卷、LB 故障状态和恢复保险 timer 均核实清理。该测试与观察器共用 Proxmox 宿主机，不代表跨物理故障域验证。
- 2026-11-19 候选发布回归 `python3 scripts/validate.py` 全量验证通过：572 项通过、1 项环境限制跳过；`git diff --check` 通过。真实 E2E 清理后隔离集群 32/32 个 Prometheus 活跃目标 UP、Atlas 故障告警为 0；三台 Kubernetes 节点 Ready、Longhorn 节点调度恢复原值，lb1 独占 `.39` VIP，API `/readyz=ok`。

- 增加三控制面 Kubernetes 故障演练，真实 HAProxy 入口验证单节点停机仍可写、失去 etcd 多数派后写入失败、恢复后原数据保持及告警解除。测试自动清理隔离资源。

- 02 配置控制面采集端点后，在限时窗口内重试端口、API `/readyz` 和静态 Pod 就绪检查。单控制面重载堆叠 etcd 导致 API 短暂超时时，部署可在组件恢复后继续。
- 独立观察器覆盖审计在读取状态库后判断样本时效，避免把读取期间刚写入的样本标为未验证。
- 三节点隔离环境通过 03 持久化首装，25 个抓取目标全部 UP，160 条告警规则健康。确认 Prometheus、Alertmanager、Grafana 重建后分别保留样本、静默和目录。真实 Longhorn 专项验证 Prometheus 跨节点重建后仍能查询原历史样本，并继续采集。

- 06 新增 `monitoring-node-network-rules`，支持按网卡配置收发带宽和丢包告警，按采集实例配置 TCP 重传与监听丢弃告警。阈值、流量下限、时间窗口及链路容量可调整。三节点隔离集群验证了网卡速率缺失触发告警、登记容量后恢复；Promtool 验证四类阈值触发与恢复。

- 逐风险覆盖报告新增 `ruleEvidence`。依据 PrometheusRule 资源 UID、规则组、告警名和静态标签核对该端点的加载结果，分别报告漏载、重复匹配、标签差异和求值错误。

- 逐风险覆盖档案增加 `riskMetrics`，可按精确指标与标签核对原始样本时间，区分样本缺失、过期、重复和无法验证。
- 同一个 `job/instance` 出现在多个抓取池时，06 使用实际目标标签核对登记样本的来源。Blackbox 受管采集配置为 exporter 和各探测目标设置独立的抓取标签，适用于目标清单与 `up` 序列。探测规则检查最近 90 秒内的样本，防止已撤销目标的旧失败值继续维持告警。

- 06 新增 `monitoring-systemd-remove --node` 变更计划，支持单节点卸载受管 systemd 指标采集器。执行时锁定并核对主机身份和受管文件摘要，保留共享指标目录和 Kubernetes 运行时服务。变更确认失败时提示输入完整计划 ID。
- 节点 systemd 规则同时检查抓取样本时间和受管 textfile 的最后写入时间。timer 停止更新文件时，即使 node-exporter 仍能抓取旧样本，规则也会发出遥测过期告警。

本版本覆盖节点、控制面、etcd、DNS、工作负载、网络、LB、Longhorn 与监控自身，并为通知、外部观察和多集群接入提供基础能力。业务专属 SLO、数据库/缓存/消息队列指标、备份与恢复监控以及实际生产业务入口仍按环境或后续版本规划；备份能力归入 0.5.0，入口控制器适配见后续里程碑。详情见[监控设计](docs/07-监控与预警/03-监控与告警功能设计.md)、[监控验证与生产故障定位清单](docs/07-监控与预警/04-监控告警验证与生产故障定位清单.md)及[产品版本路线图](docs/09-产品路线图/01-版本里程碑与能力规划.md)。

### 已实现并验证

- 通过 06 号持续运维入口预览、确认并应用采集目标、Prometheus 规则、邮件和机器人通知配置；配置计划引用 Kubernetes Secret，不把凭据正文写入计划。
- 通知向导重配接收器时复用正在使用的共享中继令牌。仅在计划成功后，才尝试清理变更前该接收器实际引用、归属匹配、且当前无引用的旧 Secret。其他会话暂存的凭据、共享中继凭据和仍有引用的旧凭据保留，删除时由 Kubernetes API 校验 UID 和 resourceVersion。
- 修正 CoreDNS SERVFAIL/REFUSED 告警的观测值。正文显示实际响应错误比例，精确到小数点后两位。隔离 CoreDNS 已验证真实查询错误、P95 延迟、指标缺失及恢复。
- 通知凭据的引用检查覆盖已安装的 Prometheus Operator 资源。ServiceMonitor 采集认证和 Alertmanager 配置使用的旧 Secret 会保留，已安装的监控 API 无法读取时也会保留。
- 节点运行时指标由主机侧受限 systemd timer 采集 kubelet/containerd 状态，并以 textfile 格式只读暴露给 node-exporter；02 新节点准备流程自动安装，03 监控插件配置采集目录及服务不可用/数据缺失规则，已有集群可用 06 的 `monitoring-systemd-collection` 计划按节点安装并失败续作。采集器不访问 Pod 内 D-Bus，也不提升 exporter 权限。2026-11-16 在 10.20.1.28–.30 隔离集群通过 06 计划为 cp1/w1/w2 安装并核实 timer、样本新鲜度及 kubelet/containerd active 状态；使用固定 kube-prometheus-stack 93.3.0 与 03 安全默认值升级后，node-exporter DaemonSet 3/3 Ready，textfile hostPath 只读挂载，Prometheus 已抓到三节点的 kubelet/containerd 指标。真实停止 w2 kubelet 后 `AtlasKubeletServiceUnavailable` 在 120 秒阈值后以中文摘要和处理建议进入 firing；恢复 kubelet 后节点 Ready、规则恢复 inactive。该次 kubelet 故障未验证外发通知送达；03 完整交互式首次安装已另在干净的一节点 Kind 集群通过。
- 通知路由支持严格标签匹配及 Alertmanager 分组参数；计划层校验和预览已覆盖责任标签、分组字段及时间间隔。真实 Alertmanager 0.35.0 三节点 E2E 通过本机临时 webhook 验证 `cluster/alertname/team` 分组、同组聚合、不同组拆分、group_wait 2.0 秒、group_interval 更新 1.8 秒及恢复通知；临时 AlertmanagerConfig、告警和接收器均已清理。critical→warning/info 抑制规则支持 06 号计划管理，并限定 `cluster` 与风险标识等关联标签；Alertmanager v2 静默支持精确条件创建、按集群归属提前结束及最长 8 小时自动过期。实集群验证了同集群同命名空间抑制、跨命名空间隔离，以及静默创建后告警 suppressed、提前结束后恢复 active；API Service Proxy 请求结束后远端代理进程已验证清理。
- 2026-11-16 在保留的 10.20.1.28–.30 三节点隔离集群完成真实通知链 E2E：临时中文 PrometheusRule 被 Operator 选择并进入 firing，Alertmanager 加载唯一匹配的受管路由，集群内接收器实际收到带规则身份、级别和处理说明的 firing payload；将表达式改为 false 后收到同一告警的 resolved payload。所有临时路由、规则、接收器和端口转发均已清理。该测试不触达外部邮箱或机器人；企微、钉钉、飞书测试群的真实渠道送达另有用户确认。
- 告警关联日志选择器按 Alloy 实际发布的 `instance=命名空间/Pod:容器` 标签检索；对只提供节点身份的告警按显式采集的 `node` 标签查询，不对缺少对象身份的告警做宽泛检索。此前三节点真实 Loki E2E 发现使用不存在的 `namespace/container/node` 标签会成功返回空结果，并据此修复。2026-11-18 用隔离三节点集群中的唯一合成日志 marker 验证真实 Loki 查询：Pod 精确查询和仅节点查询均命中；诊断正文为中文，无 Grafana 入口时不含链接，配置 HTTPS URL 与 datasource UID 时生成 Explore 链接。实际运行 Alloy v1.22.0 校验器通过，并在保留集群将 chart 1.16.0 升级至 revision 2 后验证采集。测试 Pod、Job 和 ConfigMap 已清理。relay 邮件/机器人集成测试验证最终 payload。未提供生产 Grafana 域名，实际公网 DNS/TLS 链和手机外网访问仍由部署环境验收。
- 通知中继不再要求用户预先构建或发布自定义镜像：发布计划会将经过测试的 Python zipapp 与多语言目录打包进内容寻址、不可变 ConfigMap，并使用按镜像源改写的固定 digest 官方 Python 运行时。三节点隔离集群通过真实镜像拉取、Alertmanager sidecar Ready、受管路由应用以及告警 firing/resolved E2E；真实企微测试 Webhook 接受两条中文告警（中继计数 accepted=2，unknown/retryable/rejected 均为 0）。测试规则、路由、凭据、sidecar、ConfigMap 和端口转发均已清理；Alertmanager 已恢复为 2/2 Ready。用户此前确认企微、钉钉、飞书各测试群的故障与恢复消息均已收到；本次平台应答不等同于用户再次确认收件。
- 抑制 E2E 发现旧 Chart 内置规则未以 `cluster` 作为相等标签。相同 Chart 93.3.0 使用新安全默认值临时原子升级至 revision 6 后，生成配置包含 `cluster`，三条短时告警实测跨集群 warning 保持 active；随后曾回滚到 revision 5 配置。为清除保留测试集群中的该风险，已将相同安全默认值重新应用并保留在 revision 8；当前生成配置四条内置抑制规则均按 `cluster` 隔离，三节点及全部 Pod 健康。
- 新增 03 插件菜单项 Blackbox Exporter，以加固 DaemonSet 部署到每个节点；06 号计划可登记集群内 HTTP Service DNS、ClusterIP 或 Pod IP，自动接入逐节点探测、中文故障/采集告警和限制目标范围的出口策略。10.20.1.28–.30 三节点实集群验证 9 个探测点全绿、出口受限后触发告警、恢复策略后探测全绿且告警清除。
- 规则覆盖节点就绪、控制面与 etcd 目标可用性、CoreDNS 错误率/延迟/副本数、cert-manager 管理证书到期/未就绪/指标缺失、工作负载副本状态、Deployment 发布超时、Pod Pending/容器启动失败/重启突增/OOMKilled、失败或超时 Job、逾期 CronJob 计划、HPA 扩容受限、PVC 空间、HAProxy 后端/容量及 Longhorn 卷与磁盘状态。
- 2026-11-18 在保留三节点集群经 06 只读入口验证三节点 Ready 与新鲜节点样本，Prometheus 中控制面/单成员 etcd 真实指标及默认规则健康，Cilium 三节点 Agent/Operator、IPAM、eBPF 压力和连通性指标及规则健康；Prometheus Target API 显示 46 个 active target 当前均 UP，包含节点、控制面、DNS、监控栈、Longhorn、LB metrics 和 Blackbox 探测。Target 审计规则 API 中 9 条 `R-OBS-01` kubelet 抓取规则健康且 inactive。逐风险审计输入的 16 个预期目标均健康；其余 30 个是本次未登记为预期输入的已发现目标，并非未采集目标。完整预期清单和逐风险指标映射仍未审阅闭环，相关风险状态继续 fail closed。该实测验证审计路径、活动目标状态及差异识别，不替代故障恢复或通知送达验收。
- 修复 Longhorn 磁盘实集群 E2E 将 CRD 条件 `Ready` 错按小写 `ready` 比对的问题；新增回归测试覆盖大小写变体。修复后在 10.20.1.28–.30 验证 w1、w2 实际磁盘指标分别触发容量告警、恢复阈值后告警解除，并清理唯一临时规则。此为指标与规则恢复测试，不修改磁盘/卷，也不替代 CSI 副本故障和数据完整性演练。
- 2026-11-18 使用保留的 10.20.1.28–.30 三节点隔离集群补齐真实监控 E2E：控制面/etcd 与 Cilium 指标只读检查通过；真实 Longhorn 磁盘容量规则在 w1、w2 分别完成 firing/resolved；工作负载健康用例验证 Job/CronJob 指标和 Pending Pod 告警，故障用例验证 CrashLoop 重启、OOMKilled、缺少 Secret 的容器启动失败告警，均在清理临时 Namespace 后确认告警恢复。测试前检查不存在 AlertmanagerConfig 自定义路由，未发送外部故障消息。该轮测试组未执行需 cp1 root 权限并暂停 controller-manager 的 Job 超时/CronJob 漏跑用例；后续已在销毁型 Kind 集群独立验证该场景。
- 新增 CSI VolumeAttachment 基础告警：长期未附加和状态指标缺失/过期分别告警，并带 `R-STOR-02` 风险标签及本地化中文说明。Prometheus `promtool` 覆盖触发与恢复，单元测试覆盖三种语言；2026-11-18 隔离 Kind E2E 进一步通过 hostpath CSI 控制器故障验证真实 attached 指标与告警 firing/resolved。数据来自 kube-state-metrics 的实验性 VolumeAttachment 指标；Longhorn/生产 CSI 驱动的挂载错误、attach/detach 错误详情、I/O 延迟和错误率仍需按驱动适配。
- CSI 规则加入后的完整回归：`PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate.py`，519 项通过、1 项平台限制跳过；另行运行 `tests/e2e/monitoring_rules.py --confirm atlas-monitor-e2e`，promtool 全部规则场景通过。
- 2026-11-18 在新建的单节点 Kind v1.36.1 隔离集群运行 `monitoring_operator.py --self-monitoring`：kube-prometheus-stack 安装完成，Prometheus Rules API 实际加载并报告健康的 CSI 未附加/状态遥测缺失规则；节点资源指标抓取、systemd 状态规则 firing/resolved、Alertmanager 目标移除告警/恢复也通过。测试未创建 CSI VolumeAttachment、未注入真实 CSI 故障；测试脚本确认临时集群和 port-forward 清理完成。
- 2026-11-18 再在全新三节点 Kind v1.36.1（1 control-plane + 2 worker）交互运行 `03-install-addons.sh --lang zh_CN` 首装 kube-prometheus-stack 93.4.0（Prometheus v3.16.0）。8 个监控 Pod 全部 Ready；Prometheus API 实载 160 条告警规则且健康度全为 `ok`，包含 7 条 Atlas 规则；node-exporter 三节点目标 3/3 UP、三节点系统指标均有样本；确认 kube-proxy ServiceMonitor 默认关闭、Grafana 使用 `Recreate`、受管 AlertmanagerConfig 选择器和中文告警插件说明。Prometheus 总计 25 个目标中 22 个 UP；Kind 的 Controller Manager、Scheduler、etcd 仍只监听节点回环地址，3 个对应抓取目标拒绝连接，这是 Kind 拓扑限制，不能代表 kubeadm 控制面目标状态。临时存储，首装不包含生产 CSI/LB；测试集群、Secret、port-forward 和 kubeconfig 均已清理，因此仍需目标生产拓扑首装验收。
- 2026-11-18 重跑隔离真实 etcd 三成员仲裁 E2E：全成员时写入成功；停止 1 个成员仍维持 2/3 仲裁、持续写入且无告警；再停止 1 个成员后仲裁/写入失败并触发 `AtlasEtcdQuorumOrTelemetryMissing`；恢复后确认写入数据保留且 Prometheus 告警解除。另停止/恢复同场景 Prometheus，独立观察器分别检测故障/恢复并将事件交给隔离回调。临时容器、网络和观察器状态均按所有权标签删除并核实。该 Docker 单机 E2E 不代表 Kubernetes/跨物理节点网络分区或真实平台通知送达。
- 扩展 `tests/e2e/monitoring_operator.py` 支持并严格校验本地 kube-prometheus-stack 92.22.0 Chart 归档，减少 Helm 索引网络故障对隔离测试的影响；新增 5 项 Chart 名称/版本/归档校验。2026-11-18 在全新两节点 Kind v1.36.1 集群启用 pinned hostpath CSI attach 与 external-attacher，实际停止 CSI 控制器，验证 VolumeAttachment `attached=false`、KSM 指标 0、中文 R-STOR-02 告警 firing；控制器恢复后验证附加成功、Pod Ready、指标 1 和告警 resolved。测试集群、PVC、StorageClass、临时规则与 port-forward 已清理。该测试证明样例 CSI 控制器故障路径，不代表 Longhorn/生产驱动特定行为或跨节点数据恢复。
- 本地 Chart 归档校验加入后的全量 `PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate.py` 回归通过：524 项通过，1 项平台限制跳过。
- 2026-11-18 再次在全新 Kind v1.36.1 集群运行隔离 Operator 组合 E2E，使用固定 `kube-prometheus-stack 92.22.0` 本地 Chart 归档（SHA-256 已核对）：`--self-monitoring --workload --controllers --longhorn-collection --email --robot --lb` 全部通过。真实 Prometheus Operator 选择受管规则与目标，验证节点资源和 systemd 指标、Alertmanager 目标缺失与恢复、HAProxy 后端告警、Longhorn 合成容量告警、认证通知中继失败计数触发/恢复，以及 Deployment、StatefulSet、DaemonSet 的健康→故障→恢复。该轮未启用 KEDA、未发送外部渠道消息、未注入真实 Longhorn/CSI 故障，也未验证 NetworkPolicy 强制执行。Kind 集群和 Prometheus port-forward 均由测试清理，随后确认无 Kind 集群残留；同轮 `scripts/validate.py` 为 524 项通过、1 项环境限制跳过。
- 修正诊断入口 QA 说明：菜单仅校验 Grafana 链接为格式合法的公网 HTTPS 地址；域名 DNS、TLS 证书/Secret、认证和手机公网可达性需由管理员在 Ingress/Gateway 或外部反向代理配置并另行验证，向导不安装证书或入口资源。未填写有效 URL 时继续发送有限中文日志内容，不附链接。
- 覆盖报告新增独立观察器只读 Adapter，可将指定风险映射到观察探针，并单独呈现观察器进程、探测结果和新鲜度；复用只读 SQLite snapshot，不启动服务或读取凭据。该证据不自动替代 Prometheus 的规则、指标、通知三阶段判定；无状态目录、状态库不可读、观察器停止、探测缺失或过期均不能标健康，状态库不可读时 R-OBS-04 也明确记录为未验证。新增 7 项适配器及报告序列化/判定隔离测试，包含从真实观察器运行时写入 SQLite、再由 Adapter 读取的集成测试。2026-11-18 在隔离 lb1（10.20.1.32）通过 06 安装专用临时 systemd observer，以 loopback HTTP 探针验证实际服务锁、SQLite 最近探测与 `monitoring-observer-status` 中文状态输出；随后停止服务、清除专用状态和探针，并把 `linger` 恢复为原值 `no`。这证明真实主机的 observer 安装与状态读取路径；覆盖报告 Adapter 的实际风险映射仍由 SQLite 集成测试验证，未在该 LB 上通过 `monitoring-coverage` 运行。
- 逐风险监控覆盖报告支持将预期 Prometheus 抓取目标显式映射到风险，并审计缺失、重复、down、过期或未知目标；目标清单完整性由维护者负责。目标健康只证明抓取端点近期可访问，不证明风险对应指标样本存在或新鲜；也不代表告警触发或通知送达已验证。 本机真实 Prometheus E2E 验证 Target 为 UP 但指标样本陈旧时覆盖报告仍保持未验证；停止 exporter 标记数据链路失败，恢复后重新采集。
- 新增 `monitoring-lb` 统一计划入口，将 HAProxy 来源网段配置、指标采集资源和 LB 告警规则组合为同一变更；按“来源 ACL → 采集 → 规则”分阶段执行。失败后恢复旧操作锁并续作时会核对现场，只接受原状态或完全符合目标的部分结果。单元测试覆盖阶段顺序、续作刷新和漂移拒绝；隔离 kind + Prometheus Operator E2E 实际抓到 HAProxy exporter 指标并触发中文后端告警，临时集群已清理。10.20.1.28–.30 三节点集群实测 lb1/lb2 两个 HAProxy 目标均为 up、前后端指标被抓取、告警规则正常加载；未授权 LB 来源返回 HTTP 403。对备用 lb2 的后端进行 HAProxy runtime 状态切换后，真实后端不可用告警进入 firing，恢复服务器后指标恢复且告警解除；测试通知由精确 Alertmanager 静默拦截，删除请求返回 HTTP 200，状态为 expired。2026-11-16 再以真实 lb2 HAProxy exporter 停止/恢复验证目标缺失及后端指标缺失告警触发、恢复，lb1 持续持有 VIP 且 API 健康。停止 lb1 Keepalived 后，VIP 独占迁移至 lb2，API `/readyz` 持续成功；恢复 lb1 后 VIP 返回。QQ 邮件客户端已确认新模板的触发和恢复邮件均显示为中文卡片。上述 LB 故障演练未覆盖网络分区、无持有者、重复持有及漂移频率告警。
- 03 首次安装 kube-prometheus-stack 时默认加入简体中文/繁体中文/英文的 `AtlasAlertmanagerUnavailableOrTelemetryMissing` 告警，在 Alertmanager 目标消失或全部不可用持续 1 分钟后触发。该规则填补全量 Alertmanager 目标消失时部分上游默认规则不触发的空档；若 Prometheus 自身失联，仍需独立观察者。Chart 93.3.0 的 `additionalPrometheusRulesMap` 官方 values 和模板支持该配置（[官方 values](https://github.com/prometheus-community/helm-charts/blob/main/charts/kube-prometheus-stack/values.yaml)）。在 10.20.1.28–.30 隔离集群用该 Chart 渲染并通过 `--reuse-values` 安装规则，缩容 Alertmanager 到 0 后 Chart 管理的规则真实进入 firing；恢复到 1 个副本后 Pod Ready、目标 up=1、告警恢复 inactive，自动恢复计时器停止。另于 2026-11-16 在干净的一节点 Kind 集群完整运行 03 首次交互安装 Prometheus 插件：kube-prometheus-stack 93.3.0 的全部 Pod Ready，Prometheus 载入 253 条规则（含 Atlas Alertmanager 与 systemd 规则），node-exporter 目标 up，kube-proxy ServiceMonitor 未创建，Grafana 更新策略为 Recreate。该安装验证使用临时存储，不替代多节点或生产集群首次部署验收。
- 同日再于干净的三节点 Kind 集群完整交互运行 03 首装 kube-prometheus-stack 93.3.0：三节点 Ready，所有监控 Pod Ready，Prometheus 载入 253 条健康规则及 Atlas Alertmanager/systemd 规则，node-exporter 三节点目标均 up，Alertmanager 选择器包含 Atlas 受管配置标签，Grafana 使用 Recreate。25 个 active targets 中 22 个 up；余下三个是 Kind 控制器管理器、etcd、Scheduler 未按 02 节点流程开放的 loopback metrics 端点，不据此判断生产节点接线。测试节点无法使用宿主机本地代理地址，所需镜像由宿主机拉取后导入 Kind；临时集群已删除。本结果验证三节点插件首装流程，不替代真实多控制面或可访问镜像仓库验收。
- 独立 HTTPS 观察器可通过 `--observer-ca-file` 使用私有 PEM CA 验证 Kubernetes API VIP 等 HTTPS 目标，始终执行证书校验；DNS 观察不接受 CA 参数。用户级 systemd 服务使用 `Type=notify` 与 watchdog 心跳检测卡死/无响应进程，并以独立 `OnFailure` oneshot 通过企微适配器发送本地化故障通知；主进程按策略重启，正常停止/卸载不触发故障消息。Ubuntu 24.04/systemd E2E 冻结进程后验证 watchdog 超时、中文通知投递、服务重启及恢复探测，安全移除两个 unit 并删除临时容器/镜像。通知使用本地 HTTPS 协议接收器，不代表企微平台收到此类进程故障消息，也不覆盖观察主机断电或共同物理故障域。2026-11-16 从集群外 Mac 运行实际观察器进程，使用集群 CA 成功探测 `10.20.1.31:6443/readyz` 并记录 healthy；2026-11-17 在 LB 上通过 06 入口实测集群 API VIP 故障与恢复、企微平台接受通知，服务跨 SSH 会话持续运行。观察节点与集群同处 Proxmox 宿主机，未证明物理故障域隔离。
- 企微、钉钉、飞书机器人均已发送中文故障与恢复样例，并由用户确认收到；本轮用户再次确认钉钉和飞书群均收到恢复消息。钉钉和飞书消息包含 `atlas` 关键词。SMTP 故障/恢复通知也已由用户确认收到。Alertmanager 0.35.0 隔离 SMTP E2E 验证了中文 HTML-only MIME；2026-11-15/16 在隔离集群通过 06 计划发布临时邮件路由并实际触发、恢复各发送一封 SMTP 邮件，成功计数增加 2、失败计数为 0。用户已确认两封邮件在 QQ 客户端均显示为中文卡片，包含状态、告警规则、摘要、处理建议、对象与时间。测试路由、凭据、测试告警和隧道均已清理；该结果不代表生产通知路由已配置，也不保证其他邮件客户端的展示效果。
- 工作负载规则通过 promtool 验证 Job 超时、CronJob 计划逾期、暂停排除、缺失指标抑制与恢复；10.20.1.28–.30 隔离三节点集群实测生成规则对运行超过 60 秒的 Job 触发 `AtlasJobRunningTooLong`，并在临时停止 kube-controller-manager 期间对错过计划的 CronJob 触发 `AtlasCronJobScheduleOverdue`，两条告警均带 namespace/job_name 或 cronjob 标签，CronJob 摘要为中文。测试结束恢复控制器，删除临时规则和 namespace；Prometheus 中测试告警归零，三节点重新 Ready。该验证使用短时阈值及人工构造的历史调度状态，不代表默认 3600 秒 Job 阈值、生产调度器长期故障或业务任务成功 SLO 已验证。
- KEDA/HPA 事件源监控接入同一工作负载规则变更计划，增加 HPA `ScalingActive=false`、KEDA scaler/ScaledObject 错误、上游空响应、指标延迟及 Operator 目标不可用/持续缺失告警，并支持中文邮件规则名及对象/scaler 标签；目标缺失按 `monitoring-max-age` 控制的窗口判断，避免 Prometheus 默认查询回看窗口延迟发现。单元测试、promtool 触发/恢复和去重验证、通知文本验证通过。03 安装或重配 KEDA 时会启用 Operator 指标；Prometheus 先装或后装，脚本都会核验 KEDA metrics Service、已有 ServiceMonitor 和 Prometheus 选择器，必要时自动创建受管 ServiceMonitor，不升级或覆盖 KEDA Helm release，无法确认时保留现状并提示。KEDA 2.21.0 缓存 Chart 渲染、选择器校验和 03 脚本接线测试通过；保留集群上的安装顺序实测因当前网络不可达尚未执行。固定 KEDA 2.21.0 隔离 Kind E2E 实测 ServiceMonitor 抓取、scaler DNS 故障触发中文 ScaledObject 错误告警及修复后 Ready/告警恢复，还删除并恢复 ServiceMonitor 验证 Operator 目标失联告警触发/解除；临时集群已清理。E2E 同时确认工作负载命名空间映射为 `exported_namespace`，规则和通知已据此修正。
- Deployment 发布进度规则通过 promtool 验证 `ProgressDeadlineExceeded` 触发/恢复、其他原因排除及过期样本排除；隔离 Prometheus Operator + kube-state-metrics E2E 验证不可拉取镜像导致真实发布卡滞告警，与副本可用性告警同时触发，修正镜像后恢复，临时集群已清理。探针失败、generation 对齐和回滚状态关联仍未覆盖。
- 证书规则按显式登记的 `namespace/name` 检查 cert-manager 证书剩余有效期、未就绪状态及新鲜指标缺失；CLI 通过 06 的变更计划预览、确认和发布，到期阈值默认 30 天且可调整。单测、promtool 及 2026-11-15 在 10.20.1.28–.30 隔离三节点集群对 cert-manager 1.21.3 的实际指标采集、证书签发、到期/未就绪/指标缺失告警和恢复 E2E 均已验证；不验证外部证书链、域名匹配或生产 CA/续期服务商结果。
- 10.20.1.28–.30 隔离集群只读 E2E 验证 API Server、单成员 etcd、Scheduler 和 Controller Manager 的抓取目标、实时指标样本与 kube-prometheus-stack 默认控制面/etcd 规则健康状态；cp1 通过 kubeconfig 配置的 API VIP `10.20.1.31` 完成认证 `/readyz` 与节点查询。未注入控制面或 etcd 故障；该路径不代表外部客户端、多副本仲裁或故障恢复已验证。
- 隔离 Operator E2E 通过：cert-manager 证书规则被实际 Operator 选择并触发缺失指标告警；受管邮件配置、HAProxy 后端告警、Longhorn 磁盘指标与容量告警，以及 Deployment 发布卡滞/恢复和 Deployment/StatefulSet/DaemonSet 不可用/恢复均已检查，临时 kind 集群已核实清理。该 Kind 场景不包含真实 cert-manager 发行指标或 CA 签发；真实 cert-manager 发行链路另在 10.20.1.28–.30 隔离集群通过。
- 隔离 Operator E2E 的 `--self-monitoring` 模式启用 kube-prometheus-stack 默认规则、Alertmanager 和 node-exporter；Prometheus/Alertmanager 自监控以及 CPU、内存、文件系统空间/inode、磁盘 I/O、文件句柄和时钟规则加载健康，CPU、内存、空间及 inode 样本实际采集。通过 06 目标规则登记 Alertmanager endpoint，删除 ServiceMonitor 后观察到目标缺失告警触发，恢复后告警解除；临时 Kind 集群已清理。2026-11-16 在 10.20.1.28–.30 隔离集群将 Alertmanager CR 缩至 0，使用临时 PrometheusRule 实测进程不可用告警进入 firing；恢复副本后 Pod Ready、抓取目标 up、告警在保留规则期间解除。演练使用精确 Alertmanager 静默避免外发，临时规则与静默已清理；这证明规则求值及恢复链路，不代表该演练规则已默认安装。节点阈值适用性、规则求值失败、通知中继/独立观察者在监控整体失联时送达、WAL/磁盘耗尽和长期容量趋势仍待验证。
- 新增隔离 Operator E2E `--tsdb-capacity --csi-source` 模式：Prometheus 使用实际 CSI 持久化 TSDB PVC，Prometheus 抓取 kubelet 容量样本，06 受管 PVC 规则经阈值触发/恢复；测试未填满磁盘。临时 CSI 集群已清理；真实低空间/WAL 增长和容量趋势仍未验证。
- 隔离 Operator E2E 通过通知中继 PodMonitor 的认证采集、投递失败告警触发及 5 分钟窗口后恢复；测试使用模拟中继，不发送真实渠道消息。
- 2026-11-16 将 `monitoring_operator.py` 的 self-monitoring、email、robot、LB、Longhorn collection、workload、controller 场景合并运行并通过。真实 Prometheus/Operator 选择了邮件 HTML-only 配置、凭据引用的机器人配置、HAProxy/Longhorn/中继采集目标和规则；模拟中继失败计数触发中文告警，等待 5 分钟窗口后恢复；Deployment、StatefulSet、DaemonSet 不可用告警均完成健康→故障→恢复验证。临时 Kind 集群清理已确认。此组合验证不代表真实机器人再次送达、邮件客户端视觉验收、Cilium 网络策略执行或真实 Longhorn 故障恢复；这些分别由既有独立实测或未完成项覆盖。
- 2026-11-18 在隔离 Kind v1.36.1 中重跑 `monitoring_operator.py` 组合 E2E（self-monitoring、workload、controllers、Longhorn collection、email、robot、LB、KEDA）并通过。真实 Prometheus Operator 选择受管规则和目标；验证自监控资源样本、KEDA 故障/恢复、HAProxy 后端告警、Longhorn 容量告警、认证中继失败计数触发及 5 分钟窗口后恢复，以及 Deployment 发布卡滞和 Deployment/StatefulSet/DaemonSet 不可用告警的健康→故障→恢复。平台邮件/机器人资源选择已检查，但本次未发送外部渠道消息；Longhorn 使用受控合成磁盘指标，Cilium NetworkPolicy 与真实 CSI/Longhorn 副本故障不属于本次 Kind E2E。临时 Kind 集群与 Docker 容器已核实删除；随后 `scripts/validate.py` 516 项通过、1 项环境限制跳过。
- 2026-11-18 再次运行同一隔离 Operator 组合 E2E，并扩展真实 Kubernetes 变更计划重放断言：首次发布抓取目标规则后，以相同计划 ID 重放，结果为 `already-succeeded`；测试提供的 apply 回调若被调用会立即失败，因此也证明成功计划没有二次写资源。组合中的 KEDA 抓取/故障恢复、LB 后端、合成 Longhorn 容量、认证中继计数（含 5 分钟恢复窗口）、Deployment/StatefulSet/DaemonSet 故障恢复均通过；邮件 HTML 配置和机器人凭据引用只验证 Operator 选择与 Secret 生成，未再次发外部通知。固定 Chart `kube-prometheus-stack 92.22.0` 的 SHA-256 为 `5969ad38f5b3c05065374a990327ac76bfdc4283b6a76ffe9dd5c7f20f602ad7`；全量回归 524 项通过、1 项 macOS 环境限制跳过。Kind 集群、临时 kubeconfig 和额外 port-forward 均已清理。该 E2E 不覆盖真实 SMTP/机器人送达、NetworkPolicy 执行、生产 CSI/Longhorn 副本故障或生产阈值/SLO。
- 新增 `monitoring_operator.py --workload-timeouts` 销毁型 Kind 场景，独立复核真实 Job 超时与 CronJob 漏跑告警：以 60 秒临时阈值看到 `AtlasJobRunningTooLong`、`AtlasCronJobScheduleOverdue` firing，摘要均为中文；短暂停止 Kind 单节点 Controller Manager 后恢复静态 Pod 清单，确认控制器恢复、节点 Ready、测试 namespace/规则/告警清理。首次执行发现测试备份若放在 static-pod manifest 目录中会被 kubelet 当作另一份清单并导致控制器重启；现将备份移至目录外，并保留 240 秒独立恢复 watchdog。该测试仅作用于本次自动创建且校验 Kind 标签和节点身份的集群，不覆盖生产默认 3600/900 秒阈值、业务任务 SLO 或通知送达。测试后确认 Kind 集群和 kubeconfig 删除；随后全量回归 524 项通过、1 项环境限制跳过。
- 独立观察服务可作为用户级 systemd 服务运行，持久保存探测状态和待发送事件。本轮在隔离 LB 上探测 API VIP，停止两台 LB 的 HAProxy 后记录告警并得到企微平台接受响应；恢复 HAProxy 后探测恢复并得到恢复通知接受响应。
- 三节点 Cilium/Longhorn 集群中运行仓库的真实副本故障恢复 E2E：校验双副本初始健康，移除 w1 的演练卷副本后确认 Prometheus 降级告警，恢复后确认双副本重建、告警解除及哨兵 SHA256 一致。2026-11-16 在 10.20.1.28–.30 再次通过，修复了 `kubectl exec -i` 截断测试数据的问题，改由只读 ConfigMap 种子卷提供哨兵文件；清理阶段等待确认 Longhorn 卷已删除，节点调度状态、临时命名空间、StorageClass 和规则均核实恢复/清理。
- 实际 Longhorn Manager 重复导出卷状态导致的误报已修复：规则先过滤旧样本，再按 PVC/卷/状态合并采样；PVC 名称超过 Prometheus 标签长度限制时使用短引用标签，并在注释/通知中保留完整名称。
- Cilium 监控规则扩展至 Agent/Operator 健康、IPAM 指标缺失、节点地址池使用率、eBPF map 压力和节点连通性；IPAM 与 eBPF 阈值可配置。110–114 隔离集群通过真实 06 计划发布并验证三节点指标及六条规则健康，并以临时阈值完成 IPAM/eBPF 真实 Prometheus 告警触发和恢复；10.20.1.28–.30 集群只读 E2E 又确认 cp1/w1/w2 三个 Agent、Operator、IPAM 容量/使用率、eBPF 压力、节点连通性样本新鲜，六条规则健康且未触发。未注入真实地址池耗尽或 eBPF map 故障。
- 修复 Prometheus Operator 更新受管注解时计划并发检查过于严格的问题；对写入资源版本冲突仅在 UID 与受管 spec 未变化时有限重试。
- 修复控制面指标配置阶段缺少生成版本头的问题；统一 `scripts/validate.py`、版本元数据及全量 Bash 语法检查通过。431 项 Python 测试通过；Prometheus 规则求值、Alertmanager 重试/故障恢复、Prometheus→Alertmanager→鉴权中继触发/恢复、Alertmanager 0.35.0 中文 HTML/隐式 TLS SMTP，以及 Ubuntu 24.04 systemd 独立观察器 E2E 均通过并清理临时容器/镜像。真实 SMTP 邮件的客户端样式仍待在实际发信集群复核。

### 尚待完成与已知限制

- 03 自动同步 Loki/Alloy 日志自监控，采集写入行数、字节量、重试和丢弃计数。增加指标不可用、Loki 拒收和 Alloy 投递丢弃告警。两种菜单入口均在隔离集群验证，真实日志链路故障演练验证了触发、恢复和清理。
- 03 插件 5 已增加 Loki/Alloy 引导式安装：询问保留时间、StorageClass/PVC 容量和 Pod 日志命名空间，生成配置并按 Loki→Alloy 顺序安装；Alloy RBAC 只读取 Pod、Pod 日志和命名空间。Loki 采用单副本文件系统默认值，生产 HA 仍需外部对象存储及审核过的自定义 values。Loki 21.3.1、Alloy 1.16.0 Chart 渲染及 Alloy v1.19.2 配置校验通过；在临时 Kind 集群实际安装 Loki/Alloy，并查询到测试 Pod 的唯一日志标记，测试集群已删除。
- 03 菜单 18 新增逐渠道通知配置向导；菜单 19 新增监控诊断总入口，顺序复用 Prometheus、Loki/Alloy 安装 QA，再配置通知渠道，并等待 Prometheus/Alertmanager、Loki 和 Alloy 就绪。总入口保留已完成阶段供修复后续跑，不是一个原子变更/回滚计划。QA 可选邮件/企微/钉钉/飞书、通知范围和 Grafana HTTPS 地址；密码、Webhook、签名密钥通过隐藏输入，经 `kubectl create -f -` 写入随机命名 Secret。每条通知路由生成 06 变更计划，输入完整计划 ID 才应用；取消会删除未使用 Secret，应用结果不确定时保留凭据供核查。中继代码以内容寻址、不可变 ConfigMap 发布，搭配固定 digest Python 镜像并按所选镜像源自动改写地址。单测覆盖 Secret 标准输入、官方 Webhook 主机校验、邮件接收器校验、空计划 ID 取消和总入口取消状态；Kind 和保留的三节点集群均已验证 Loki/Alloy 安装与日志查询。三节点实跑发现并修复 Prometheus/Alertmanager StatefulSet 名称硬编码问题。2026-11-18 在三节点集群完成实际中继镜像拉取、sidecar Ready、菜单 18 对应通知计划应用、Alertmanager 规则选路及真实企微 Webhook firing/resolved E2E；平台接受两条中文消息，中继成功计数 2、失败计数 0。临时资源已清理，Alertmanager 恢复为 2/2 Ready。另通过真实 Loki E2E 验证 Pod 与节点级 marker 关联、通知有/无 HTTPS Explore 链接分支。
- 2026-11-18 菜单 19 临时 Kind 集成验证：Prometheus、Loki、Alloy 安装及就绪检查通过；通知配置在收集 SMTP/Webhook 凭据前发现缺少 02 阶段创建的集群配置对象并安全停止，已创建的临时 Secret 全部清理。新增通知配置前置检查，先确认配置对象存在且与当前集群匹配，再询问凭据。该轮验证不代表完整 02→03 首装或通知端到端通过。变更后 `scripts/validate.py` 为 518 项通过、1 项环境限制跳过。
- 覆盖矩阵静态复核发现跨节点 Blackbox 故障告警缺少风险身份，现为 `AtlasNetworkProbeFailed` 标注 `R-NET-03`，并将采集目标缺失告警标注 `R-OBS-01`；网络探测专项测试通过；修改后 `scripts/validate.py` 全量回归 518 项通过、1 项平台限制跳过。
- kube-proxy 默认 metrics 地址为 loopback，当前没有安全的远程采集配置；部分控制面采集目标也需按实际节点配置。Prometheus 的不可达/目标缺失规则会报告这些监控缺口；不要为消除告警而直接暴露未受保护的 metrics 端口。
- 临时集群与观察节点都在同一台 Proxmox 主机，未验证物理主机、机房或广域网故障域隔离；观察者独立运行不等于其宿主机独立。
- 完整生产风险覆盖审计及监控自身在真实故障下的长期稳定性仍需验收；机器人测试使用的是测试群，不能代表生产路由、值班责任和限流行为。
- Prometheus/Alertmanager 默认自监控规则已验证加载和健康，Alertmanager scrape 目标缺失及 使用临时规则的 Alertmanager 进程缩容至零告警已在隔离环境验证触发/恢复；TSDB 持久卷实际容量指标和阈值型 PVC 告警已验证触发/恢复，但未制造真实低空间或 WAL 增长。规则求值失败、独立观察者在监控整体失联时的通知送达及容量趋势仍未验证。
- 其他 KEDA 版本、生产环境网络策略及 HPA/KEDA 外部指标源全部失效仍待验收。
- kubelet/containerd systemd 状态采集已接入部署和运维脚本；10.20.1.28–.30 隔离三节点环境已通过 06 计划、03 安全默认值和真实 w2 kubelet 故障/恢复验证。Ubuntu AppArmor 会阻止非特权 node-exporter Pod 直接访问 D-Bus，因此产品采用主机受限 timer，不关闭容器隔离。03 完整首次安装交互流程已在干净的一节点和三节点 Kind 集群验收；保留三节点集群已完成监控组件集成、真实通知与日志诊断 E2E。该证据不代替其他生产拓扑的首次部署和参数验收。
- 规则发布或 SMTP/企微平台接受请求不等同于所有接收人实际收到；告警阈值和通知策略仍需按业务 SLO 与值班责任配置。

## 0.3.0 — 2026-11-10

**发布日期：2026-11-10；比较基线：`v0.2.0`；发布 tag：`v0.3.0`。** 本次次版本将五个部署/管理入口
扩展为六个，新增持续运维、受控变更、备份演练与离线交付。源码及 14 个入口/阶段版本统一为 `0.3.0`。

### 版本重点

- **安全收尾**：核对部署授权、配对服务和管理入口；区分部署、日常运维、只读巡检身份，形成可预览的收尾计划。
- **备份与恢复**：统一登记 etcd 快照、集群配置与证书、持久卷和应用数据的保护范围；提供备份检查、恢复计划与隔离环境演练，记录恢复边界。
- **统一配置与变更预览**：在现有部署计划上增加版本化配置、导入导出、实际环境差异、操作锁与断点恢复；凭据独立引用。
- **节点与组件生命周期**：受控新增、维护、替换和退出节点，按兼容约束逐步升级组件；检查存储副本、PDB 和 etcd 仲裁等前提。
- **统一报告与诊断**：每次操作生成执行编号、结构化结果和易读报告，区分失败、跳过、未验证，提供脱敏诊断材料。
- **物料锁定与离线交付**：记录组件/Chart/包版本和镜像 digest，导出物料及校验清单，支持企业仓库与离线交付。
- **工程支撑**：将配置规则、计划、执行、报告职责分开，保留 Shell 向导；复用 messageKey 国际化，逐步接入自动化回归与可销毁环境验证。

详细功能及边界见 [运维功能定义](docs/04-持续运维/01-持续运维功能定义.md)，第一批设计见
[运维核心设计](docs/04-持续运维/02-持续运维核心架构设计.md)。

### 已实现能力

#### 06：持续运维入口

- 分组菜单与等价 CLI，覆盖巡检、安全收尾、配置、变更、备份/恢复、物料与外部数据保护证据；新增文本使用英文、简体和繁体 messageKey。
- schemaVersion 1 集群 JSON：模板、校验、私密导入/导出、revision 并发保护和实际漂移报告；拒绝重复/未知字段、凭据、网络/身份冲突和可执行配置正文。02 可消费同一配置。
- 不可变执行计划绑定完整 SHA、集群 UID、观察指纹和有效期；执行前重新检查，成功操作幂等；失败/取消保留步骤，继续操作重新规划和确认。
- 固定 kube-system CAS 锁协调多台管理机，不按超时自动抢占。显式恢复需要旧 owner、resourceVersion 及旧作业停止确认；02 原生阶段和 05/06 主机写入共同遵守互斥。

#### 安全、备份和诊断

- 安全收尾在独立 SSH 新通道验证保留账号的无缓存 sudo 后，才删除工具临时授权/配对服务；失败回退，只处理工具拥有的规则，保留管理员自建授权。
- age 公钥加密配置与控制面 etcd 备份；固定源白名单、私密目录、原子发布、密文/大小/逐文件完整性与身份检查，受主机锁保护。
- 隔离恢复与演练只写新目录，使用匹配版本的 etcd 工具；可启动独立 mTLS etcd 读取原集群身份，随后停止自有进程，明确区分校验、恢复和服务验证。
- 外部卷/数据库保护证据可按密文大小/SHA、创建时间和资源 UID 登记；篡改拒绝、重复登记幂等、重建资源后旧证据过期，新登记保留历史。此能力不宣称解密或应用恢复成功。
- 每次操作形成私密 JSON/文本报告；02 部署/审计、04 选中动作有原生结果摘要。诊断包只采集 9 类 API 白名单，设大小上限和 SHA 清单，排除 Secret、私钥、kubeconfig 正文及原始日志。

#### 节点与组件生命周期

- worker 加入、cordon/drain/uncordon、退出和不同身份替换，检查 PDB、存储、CSI/Longhorn、本地 PV 与维护归属；短期 join 凭据在私密通道使用并撤销。退出保留磁盘与身份，不自动 reset/格式化。
- Kubernetes 同 minor 补丁升级：安装包预检、逐节点维护、状态记录及中断继续；每个控制面升级后恢复原 CoreDNS 副本和调度布局并等待就绪，再进入下一节点，避免默认布局阻断后续 PDB。继续计划保留原布局，实际驱逐仍重新检查预算。升级前的加密备份与恢复演练由独立选项完成，不自动代替运维备份决策。
- Cilium 同 minor 补丁升级绑定 Chart SHA/原 values 指纹，先按 digest 准备所有节点镜像，再按节点替换 agent/envoy；operator 使用零不可用预算，遵守服务端 PDB，最后恢复 Helm/API 原更新策略。
- 组件默认值集中到 `scripts/lib/component-versions.json`：Kubernetes v1.36.6、Cilium 1.20.4、Helm v3.18.6、cri-tools v1.36.0。读取默认值不自动升级既有集群。

#### 离线交付与企业仓库

- Ubuntu 24.04 amd64 基座物料包含完整 APT 依赖闭包、签名本地仓库、OCI blob、Cilium Chart、官方校验 Helm 和确定性源码包；物料 ID 绑定配置及所有制品，导入前验证格式与签名，激活另一 ID 时拒绝覆盖。
- 01/02 使用同一锁定物料，不回退公共下载；离线准备不依赖公共 DNS，02 校验全部节点物料 ID，并在安装前检查 Service 地址的内部路由；containerd 沙箱镜像在初装时按 kubeadm 实际要求对齐，避免默认 pause 版本缺失。
- 确定性源码包保留项目 MIT 许可证及 Helm/Cilium 上游许可文本；包内哈希用于完整性，物料 ID 仍需通过可信渠道核对来源。
- 企业 HTTPS 仓库使用独立公开 CA、原子 hosts.toml 和计划指纹，验证传输；配置变更不重启 containerd。新 worker 继承经指纹验证的公开信任配置，凭据由业务独立管理。

#### LB 与工程质量

- stats 默认仅本机监听，可显式设置管理 CIDR；05 跨 RPC 主机预留和事务日志持久保存，配置原子替换，实际重启后仍保留恢复依据。部分发布回退接受旧/新文件混合，遇到第三方修改则停止。
- 02 的配置、配对、物料、原生阶段、运维桥接和验收拆为 `scripts/deploy/` 模块；06 按能力分离规则、计划、适配器、效果和报告，05 分离管理协调器、远端执行与文件发布。
- 增加统一 `scripts/validate.py` 和 Ubuntu/macOS、Python 3.9/3.12 CI；真实环境驱动另有确认、归属标记、日志保护和单批资源约束，不在 PR 中使用服务器凭据。
- 发布校验使用 Ubuntu 24.04 和明确的 `macos-15-intel` runner，避免已不受支持的 macOS 14 标签导致任务在运行前被取消；不改变部署目标平台。
- 修复 AppleDouble 隐藏文件误识别语言目录、压缩扩展头与 Python 3.12 tar 行为差异、超大/过多归档元数据、保护路径检查顺序和重启后事务预留丢失问题。
- 修复 02 首次部署后 LB 的空/`none` 磁盘标记差异导致只读重跑误报冲突；Kubernetes 数据盘、接口与其他真实配置差异继续拒绝。
- 修复 04 的提前校验失败可能被摘要记为通过；动作退出码和命令错误计数共同决定结果，主动跳过继续单独计数。
- 升级包管理辅助代码随 06 固定分发，兼容未安装新版辅助目录的 0.2.0 节点；仍激活离线物料时提前拒绝补丁预览，避免在驱逐后才发现跨物料迁移不受支持。

### 已知限制

- 不包含控制面成员增删、跨 minor 升级、通用应用数据库导出/恢复、认证仓库凭据管理、异地灾备编排或生产原地 etcd 恢复；卷和应用保护必须单独设计。

## 0.2.0 — 2026-10-22

**发布日期：2026-10-22；比较基线：`v0.1.0`；发布 tag：`v0.2.0`。** 本次为功能扩展的次版本升级，
将工具集从四个部署/检查入口扩展为五个部署与运维入口。源码版本和脚本内嵌版本
同步为 `0.2.0`，提供对应 Git tag 与 GitHub Release。Kubernetes **v1.36.5**、
Cilium **1.20.2** 保持原有版本，本次升级不代表组件升级。

### 新增

#### 05：HAProxy / Keepalived 运维控制台

- 新增 `05-manage-lb.sh` 与 Python 协调器 `scripts/lb/manager.py`，可从能 SSH 到 LB 的管理机运行，复用当前用户的 SSH 凭据，在目标节点通过 `sudo -n` 执行管理操作。
- 实时状态默认每 3 秒刷新：节点连接、IPv4/VIP、服务状态、配置校验、HAProxy 前后端 UP 状态、当前连接和累计会话；Ctrl+C 返回菜单。
- 支持受管业务 VIP 的增删查，以及 HTTP/TCP 端口转发规则的增删查；可选择业务 VIP 或通配地址监听，HTTP 模式可配置 WebSocket tunnel timeout。
- 自有 LB 清单支持批量替换、添加、移除与设置 SSH 用户；外部 LB 使用独立登记清单。清单不等同于节点安装，外部登记不包含厂商 API 管理。
- 修改前要求节点配置与受管状态一致，并在所有目标上生成候选配置、运行真实校验；应用前创建 root-only 备份，逐节点 reload，失败时尝试回滚已处理节点。
- 仅维护标记内的业务路由和 VIP，保护 Kubernetes API VIP、已有非受管前后端及其他 Keepalived 配置。

#### 02：集群审计与旧节点基线修复

- 新增 `--check` 只读审计：检查阶段一基线、运行时及 LB 依赖、SSH/机器身份唯一性、hosts 映射、实际组件存储；区分失败与未验证并给出建议。
- 新增 `--repair` 六项修复：`hosts`、`node-keys`、`ssh`、`sudo`、`adopt`、`host-keys`；支持 `--items`、`--dry-run`、逐节点 root 备份、SSH 新连接校验、局部回退和修复后重新审计。
- `01 --pair` 支持没有阶段一完成状态的旧节点，仅启用配对服务；`02 --check --pair` 支持批量配对、保存审计清单及主机公钥、本机直接采集和按需 sudo 密码授权。
- 阶段二分发审计/修复辅助程序到节点；登记旧节点前必须验证真实基线，轮换主机密钥要求额外确认。

#### 02：API 与应用 LB 自动配置

- 新增可选 `APP_INGRESS_VIP`：与 API VIP 分别登记，检查与已登记节点/API VIP 的冲突，保存部署计划并分发到所有 LB。
- 业务 VIP 合并到受管状态；重跑保留 05 写入的路由和 VIP。安装阶段不固定 Gateway NodePort，业务服务就绪后再通过 05 配置转发。
- 新增 `setup-lb.sh --check-only`，检查配置、服务和关键前提，不修改配置或重启服务。
- 部署结束验证两项 VIP 的实际归属：要求连续三次观察到同一个登记 LB 单独持有 VIP，并保留 Kubernetes 节点到应用 VIP 的 ARP 检查。

#### 通用国际化与存储检测

- 新增 `locales/en.json`、`zh_cn.json`、`zh_tw.json`，通过 messageKey、共享 Bash/Python 查询接口、语言发现和回退机制读取文本；新语言可扩展独立 JSON 文件。
- 01、03、04 的主要交互以及 02 的核心帮助、部署提示、存储计划和进度迁移到语言目录；新增 05 使用同一目录。01/02 保存语言供重启续跑使用。
- 新增统一 `storage.py`：识别分区、LVM、RAID 与实际 containerd/kubelet/etcd 路径；已有组件挂载保留，其他独立挂载空间经确认后复用专属子目录，无独立盘使用系统盘。
- 新增 `scripts/sync-storage.py` 与 `scripts/sync-addon-sources.py`，维护脚本中的存储和插件下载辅助载荷。
- 版本同步工具将 05 纳入完整入口检查，统一维护五个入口与八个内部阶段脚本的内嵌版本。

#### 03：16 项插件与自动授权

- 新增 Headlamp 与 Longhorn，插件总数由 14 增至 16。Headlamp 移至菜单首位并建议优先安装，默认保留 ClusterIP 与只读访问。
- 在安装器启动及每个插件安装/升级成功后，若 Headlamp 服务账号存在，按已部署插件的 CRD API 组及真实资源名自动维护只读 ClusterRole/Binding；仅授予 `get/list/watch`，不增加写权限或 Secret 读取。
- Longhorn 安装前通过阶段二 SSH mesh 在所有 Kubernetes 节点安装 `open-iscsi`、`nfs-common` 等主机工具，启用 `iscsid`、加载并持久化 `iscsi_tcp`，避开不兼容的 open-iscsi 2.1.12。
- Longhorn 副本选择说明 1/2/3 份副本的冗余、近似空间开销、节点放置条件及仅影响新建卷；副本选项按节点数限制。
- Envoy Gateway 就绪后可选复用/创建 GatewayClass，为已有 Service 创建基础 HTTP Gateway/HTTPRoute；校验资源名称、命名空间、Service 端口及 GatewayClass 接受状态，拒绝覆盖现有路由。

#### 03：统一下载源与版本匹配

- 启动顺序调整为语言 → 下载源 → 集群检查 → 插件菜单；主菜单 `S` 可切源，保存选择供全部插件安装/升级共用。
- 支持官方源、DaoCloud、1Panel + DaoCloud、Google Docker Hub 缓存和自定义企业代理；统一处理 Helm HTTP 索引/包、OCI Chart、常见仓库镜像、钩子 values、Prometheus operator 镜像参数及 Envoy 数据面镜像。
- 按实际 Kubernetes 服务端版本为全部 16 项插件筛选稳定 Chart，使用 Helm 校验 `kubeVersion`，为已适配的五个项目解析官方兼容表；手动选版后复核下载包并按可用 API 预渲染。
- 区分上游支持范围、Chart 声明（`CHART`）与缺少依据（`UNKNOWN`）；明确不匹配时停止，未知兼容性默认拒绝继续，不将 Chart 声明当作完整上游验收。
- 自动从所选 Chart 的 appVersion、官方版本建议/Chart 默认值或对应源码解析 Envoy Proxy 镜像，取消固定版本及镜像坐标强制输入；元数据不足时停止，不猜测版本。

### 修复

#### 主机准备与集群编排

- 修复 01 首次重启前未安装语言运行库/目录，导致续跑出现 `atlas_printf: command not found`；现在重启前与配对服务准备时均持久化所需依赖。
- 修复 `sshd -T | grep -q` 在 pipefail 下可能因 SIGPIPE 将正确配置误判为失败；改为采集完整生效配置后匹配。
- 修复 02 审计批量粘贴重复提示和无效行被遗漏的问题；无效输入阻止开始审计。
- 修复 sudo 启动审计时选错 SSH 私钥：使用原登录账号身份，缺失显式密钥直接报错，缺少远端凭据时标记未验证。
- 控制平面 join 显式传入登记 IP 和 API 监听端口，保持与 LB 后端一致。
- 统一初始化、加入与节点准备阶段的存储判断，拒绝覆盖已有组件数据或遮盖已有挂载。

#### LB 安装、重跑与回退

- 自动补齐 `haproxy`、`keepalived`、`psmisc`、`python3`；修复健康检查依赖 `/usr/bin/killall` 缺失的问题。
- 安装 root:root 0755 的专用健康检查包装程序，配置 `script_user root`、`enable_script_security` 和合法 MASTER/BACKUP 状态，消除缺失依赖、无效 SLAVE 状态及脚本安全校验错误。
- 使用有效的八字符共享 VRRP 认证值；重跑时从已有 `VI_KUBE_APISERVER` 读取一致的认证值，不因计划文件清理而生成新值，节点之间不一致时停止。
- 持久化并应用 `net.ipv4.ip_nonlocal_bind=1`，使备用节点能够提前监听业务 VIP，避免 HAProxy reload 出现 `Cannot assign requested address`。
- 写入前校验两份候选配置，备份配置/受管 JSON/sysctl 文件及服务状态；失败或可处理的中断恢复文件、内核参数和原有服务/自启动状态。
- 相同配置重跑不重启已运行守护进程；自动纠正配置和健康检查程序权限。保留业务路由与 VIP，标记区块损坏或与管理数据不一致时停止。
- 加强 IP、网卡、后端及端口校验，拒绝 API 端口与 stats 7000 冲突；最终检查实际服务运行和自启动状态。
- 不再仅依赖 ARP 判断 VIP 正常，避免两个登记节点同时持有 VIP 时误判成功。

#### 插件安装

- 剔除镜像后处理器产生的空 YAML 文档，修复 Helm `invalid Yaml document separator: null`。
- 修复 Longhorn APT 候选查询的 SIGPIPE/141 失败及 open-iscsi 2.1.12 识别；失败报告安全候选与远端出错行。
- 允许已核实的 DaoCloud HTTPS CDN 跨域跳转，未知域名仍拒绝；显示域名诊断而不输出签名参数。选源失败不静默回退。
- 统一 Chart 配套镜像处理；保留额外/已有 values 可覆盖镜像的提示，Envoy 升级重新解析普通上游镜像。
- 修复选择下载源时 `ask` 未定义导致交互中断的问题，提前定义共用提示函数。

### 兼容性与升级

- `0.x` 阶段继续记录行为变化，不承诺稳定版接口兼容。01～05 入口和内部阶段脚本统一使用本版内嵌版本；版本查询仍可独立执行。
- **复制完整项目目录**：界面运行依赖 `locales/` 与 `scripts/lib/`；02 还依赖阶段脚本/辅助程序，05 依赖 `scripts/lb/`。旧版单文件复制操作不适用于本版正常交互。
- 插件编号因 Headlamp 排在首位而变化，以插件名称识别操作，不沿用旧编号。Headlamp 是推荐先装，并未禁止其他安装顺序。
- 02 的应用 VIP 可留空，保留仅 API VIP 行为；API 与业务 VIP 使用同一 VRRP 实例，故障时一起切换。Gateway NodePort、DNS、TLS、ACL 按实际环境后续配置。
- 服务器状态目录和服务继续使用 `k8s-deploy` 标识；源码升级不自动更新服务器副本、重启服务、升级 Kubernetes/Chart 或迁移数据。更新前核对续跑状态，备份部署计划及 LB 配置。
- 旧节点先执行 `--check`，按预览选择 `--repair`；sudo 修复会授予指定用户 root 免密权限，主机密钥轮换要求外部客户端核对新指纹。

### 已知限制

- 本地环境配置不能直接代表所有云网络、部署拓扑、容量和跨地域灾备场景；部署前仍需按目标环境检查网络、存储和故障域。
- 国际化架构已建立，部分 02 详细诊断、check/repair 辅助程序、03 远端引导和内部阶段输出仍待迁移；不宣称全部脚本输出已经翻译。
- 05 为试用功能：没有外部厂商 API、同端口域名/路径路由、TLS 终止或多集群灾备编排；长连接 timeout 不保证跨 LB 故障保留既有连接。
- HAProxy stats 7000 当前仍需管理网 ACL；Longhorn 不替代异地备份，公共下载源不保证可达，自定义 values 的镜像兼容性须单独确认。

## 0.1.0 — 2026-10-02

**发布日期：2026-10-02。** 这是 Atlas KubeKit 的首版功能基线，面向 Ubuntu Server 24.04 amd64，覆盖主机准备、内网集群编排、可选插件安装和日常检查。项目采用 MIT 许可证，版权署名为 **Atlas Richie**。

这是 Atlas KubeKit 的首个公开版本。当前源码锁定 Kubernetes **v1.36.5**、Cilium **1.20.2**；这两个组件版本与工具集的 `0.1.0` 独立管理。

### 新增

#### 01：节点准备与重启续跑

- 提供 `01-prepare-node.sh`，在每台机器分别检查 Ubuntu 版本、amd64 架构、内存、系统盘可用空间及 CP 的 CPU 条件。
- 支持交互选择 `lbN/cpN/wN` 节点名、网卡、SSH 管理账号和管理员公钥来源。
- 提供 `keep/static` 网络模式：保留现有网络，或配置静态 IPv4、网关及 DNS；静态模式默认使用阿里云 DNS。
- 更新系统与基础依赖，通过 systemd 在重启后继续主机准备流程。
- 配置独立 SSH 主机身份和节点密钥，验证节点公钥登录后禁用 SSH 密码登录；为阶段二提供非交互 sudo 前提。
- 自动发布登录用户的 `~/.k8s-deploy/node.conf`，提供一次性内网配对服务与包含 IP 的配对码。
- 提供 `--status`、`--code`、`--pair` 及 `--config-only` 等操作；仅补生成配置要求阶段一已经完成。

#### 02：集中编排 Kubernetes 集群

- 提供 `02-deploy-cluster.sh`，从任意已完成阶段一的登记节点发起部署，无需管理员电脑直接访问全部机器。
- 持续登记配对码或已互信的节点 IP，输入 `done` 结束；数量不固定为实验室示例的 8 台。
- 全节点配置 hostname/IP 映射与 SSH 公钥互信，通过 hostname 拉取本机清单并核对角色、IP、网卡和公钥。
- 检查 machine-id、product UUID、MAC 及 SSH 主机身份；可修复符合前提的重复 machine-id/主机密钥，UUID/MAC 冲突则停止并要求处理。
- 按 hostname 分工安装基础依赖、containerd 与 Kubernetes 包，配置 swap、内核与 cgroup 条件。
- 配置 HAProxy/Keepalived API 入口，初始化 cp1，安装 Cilium，自动生成和分发短期 join 凭据以加入其他 CP/worker。
- 将 CoreDNS 分散至登记的控制平面节点，并根据控制面数量配置 Cilium operator 副本。
- 支持可选独立数据盘：默认仅选择唯一符合条件的空白非系统盘，没有时使用系统盘；不自动格式化有数据或身份不明确的磁盘。
- 保存部署计划、处理适用的重启续跑并在失败后保留现场；已有状态满足条件时跳过对应步骤，冲突状态会停止。
- 完成节点 Ready、Cilium/CoreDNS rollout、etcd Pod、LB 服务与 VIP API 检查后记录完成标记，并清理短期 join 凭据。

#### 03：14 项可选插件

- 提供 `03-install-addons.sh`，通过一级插件菜单、二级功能说明、安装确认及专属配置完成 Helm 安装。
- 包含 Metrics Server、cert-manager、Prometheus stack、Loki + Alloy、Argo CD、External Secrets、ExternalDNS、Velero、NFS CSI、Envoy Gateway、Kyverno、MetalLB、KEDA、Traefik Ingress。
- 支持中文/英文启动选择、`--lang en|zh` 和菜单内 `L/l` 切换；VNC 环境可使用英文。
- 安装前显示 Chart 版本供确认，已有 Helm release 默认跳过，选择升级后再执行。
- 按插件需求询问副本、存储、地址池、DNS/provider 和 Helm values 等配置；业务路由、Issuer、备份计划与外部凭据由用户提供。

#### 04：按角色巡检与可选功能验证

- 提供 `04-verify-cluster.sh`，根据 `cpN/lbN/wN` 及本机工具过滤检查项，可用菜单从 1 连续编号。
- 所有节点可查看主机、身份、磁盘、本机服务和 SSH 互信；CP 提供全局节点/Pod、API、etcd、CoreDNS、Cilium、证书、存储、指标和插件查询。
- LB 提供 HAProxy/Keepalived、后端 TCP、VIP 与 API 入口检查；worker 提供运行时、CNI 和到 API 的连通性检查。
- 提供经确认后创建临时资源的跨节点 Pod/Service/DNS 和 PVC 写入/重建读取检查，以及 HTTP/TLS 响应检查。
- 支持与 03 一致的中英文语言选择，并区分命令失败、缺失工具跳过与完整功能验收；LB 故障切换提供手工指引。

#### 版本管理与项目组织

- 新增 `VERSION` 作为唯一维护的项目版本来源，初始值为 `0.1.0`。
- 全部 12 个 Bash 脚本支持 `--version`/`-V`；版本查询在部署检查前退出，无需 sudo、交互终端或集群依赖。
- 通过 `scripts/update-version.py` 将版本自动写入脚本，支持 SemVer 校验、新版本更新和只读 `--check` 一致性检查。
- 单文件复制和服务器安装副本使用内嵌版本，不依赖旁边的 VERSION 或 Git；入口帮助/菜单展示工具集版本。
- 根目录保留 01～04 入口，8 个内部部署脚本集中在 `scripts/stages/`；用户指南按功能域放入 `docs/`，维护者流程单独组织。
- 提供 MIT 许可证、贡献与安全说明、行为准则、Issue/PR 模板、Git 忽略规则及文件格式配置。

### 兼容性与使用说明

- 这是首个版本，没有上一发布版本可比较；`0.x` 阶段的接口和部署行为仍可能调整，后续变化会在对应版本说明中记录。
- 02 优先读取新仓库的 `scripts/stages/`，同时兼容旧平铺包和 `/usr/local/lib/k8s-deploy/` 安装目录。
- 保留 `/var/lib/k8s-deploy/`、`~/.k8s-deploy/` 及已有 systemd 服务名，不因项目命名变化而迁移部署状态。
- 02 发起节点必须持有完整内部脚本包；01、03、04 可单独复制。源码更新不会自动覆盖服务器副本。
- 重跑要求先处理具体失败原因；脚本不会自动重置集群，也不保证所有部分安装状态都能自动修复。

### 已知限制

- 当前部署目标仅为 Ubuntu Server 24.04 amd64；其他发行版、架构、离线部署及云负载均衡适配不在首版支持范围。
- HAProxy/Keepalived VIP 需要相应二层 VRRP/ARP 网络条件；`keep` 模式保留云 IP 不代表云 VPC 支持 VIP 漂移。
- 全节点当前要求相同 SSH 管理员用户名；管理员 SSH 互信和非交互 sudo 的权限收紧需在部署收尾单独规划。
- 软件包、容器镜像及 Chart 的下载网络需自行提供；插件使用的外部 NFS、对象存储、DNS 与 Git 服务不由脚本统一创建。
- 插件控制器就绪不等于业务配置完成；节点 Ready 或 Pod Running 不代替网络、数据持久化及恢复验收。
