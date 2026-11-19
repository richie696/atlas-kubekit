# Atlas KubeKit

**By Atlas Richie** · GitHub repository: `atlas-kubekit`

**当前版本**：[`0.3.0`](https://github.com/richie696/atlas-kubekit/releases/tag/v0.3.0) · **发布日期：2026-11-10** · [本版变更](CHANGELOG.md#030--2026-11-10)。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

面向 **Ubuntu Server 24.04 amd64** 的 Kubernetes 部署与运维脚本集。通过交互选项完成主机准备、高可用集群部署、16 项插件、巡检、HAProxy/Keepalived 管理和持续运维。01～06 六个入口覆盖“准备、部署、管理、备份、变更、诊断、离线交付”，减少重复手工配置。

Interactive Kubernetes deployment and operations toolkit for Ubuntu 24.04: node preparation, HA cluster deployment, audits and repairs, 16 add-ons, role-based checks, and HAProxy/Keepalived management with extensible locale catalogs.

## 目录

- [能力与适用范围](#能力与适用范围)
- [0.2.0 新特性](#020-新特性)
- [0.3.0 新特性](#030-新特性)
- [0.4.0 候选发布](#040-候选发布监控与告警)
- [部署流程](#部署流程)
- [运行要求](#运行要求)
- [快速开始](#快速开始)
  - [修复已运行的旧集群](#修复已运行的旧集群)
- [升级现有环境](#升级现有环境)
- [入口脚本](#入口脚本)
- [变更记录](CHANGELOG.md)
- [产品概览与架构](docs/00-产品概览与架构.md)
- [用户文档](#用户文档)
- [贡献与问题反馈](#贡献与问题反馈)
- [许可证](#许可证)

## 能力与适用范围

- **01：主机准备**。检查系统、升级依赖、配置 hostname/网络/SSH，重启后由 systemd 续跑，生成本机 `node.conf` 和一次性配对码。
- **02：集群部署**。从任意已准备节点登记其他节点，配置 hosts 与双向 SSH 互信，检查克隆身份，按 `lbN/cpN/wN` 分工安装、初始化和加入集群。
- **02：只读合规检查**。`--check` 按清单核对阶段一主机基线、阶段二节点依赖、SSH 主机/节点密钥唯一性及 hosts 映射；逐项报告不符合项与处理建议，不修改节点。
- **02：旧集群基线修复**。`--repair` 提供六项修复菜单、只读预览、计划确认、逐节点备份和修复后审计；不用重新执行安装流程。
- **03：可选插件**。17 项交互菜单，统一下载源与 Kubernetes/Chart 版本检查，覆盖监控、逐节点 Blackbox 探测、日志、GitOps、备份、NFS/Longhorn 存储、Headlamp 控制台、策略、扩缩容、Gateway 和 Ingress。
- **04：检查与验证**。按节点角色展示连续编号菜单，检查主机、LB、控制面、运行时和插件；网络/PVC 功能检查需确认后创建临时资源。
- **05：LB 运维管理（试用）**。实时查看自有 LB 服务与配置状态，管理受控业务 VIP 和 HAProxy 端口转发规则；外部 LB 单独登记。详见 [LB 运维管理器说明](docs/03-负载均衡与业务入口/01-LB运维管理指南.md)。
- **06：持续运维**。统一配置/漂移、受控计划与 CAS 锁、安全交接、控制面加密备份和隔离演练、worker 生命周期、补丁升级、诊断包、保护证据与锁定离线物料。
- **0.4.0 候选发布：监控与告警**。部署配置可标记集群用途为 `monitoring` 或 `application`：监控集群部署 Prometheus、Alertmanager、Grafana、Loki 和通知服务；应用集群按需部署轻量采集 Agent，将指标和日志送往监控集群。整套方案仍在 Kubernetes 中运行；监控集群与应用集群是部署角色，便于后续集中接入多集群。候选回归 572 项通过、1 项环境限制跳过；正式版本仍为 0.3.0，0.4.0 尚未创建正式提交、tag 或 Release。详见[监控设计](docs/07-监控与预警/03-监控与告警功能设计.md)和[变更记录](CHANGELOG.md)。
- 独立数据盘可选：已用于集群组件的挂载自动保留；其他已挂载的独立空间先询问，确认后保留原文件系统和文件，在专属子目录存放数据；空白盘按计划初始化，没有独立盘则使用系统盘。
- 外置 messageKey 语言目录提供英文、简体中文和繁体中文，并支持扩展新语言；主要交互已迁移，部分诊断与内部阶段输出仍为原有语言。
- 保存部署计划和完成标记，失败后可在修复问题后重跑；已有状态冲突时会停止，重跑不保证自动修复所有故障。

新装默认 Kubernetes **v1.36.6**、Cilium **1.20.4**，由统一组件版本目录定义。更新工具不会自动升级已有集群；集群和插件升级分别通过明确操作执行。插件 Chart 在安装时查询版本并供用户确认。

首发目标平台为 Ubuntu Server 24.04 amd64。其他云厂商、拓扑、容量和灾备方案需按实际环境设计；外部镜像、软件包和 Chart 必须能从部署节点访问。

## 0.2.0 新特性

本版相对于 `v0.1.0` 扩展部署后的运维能力，并补齐多项首次安装、重启续跑和重复执行问题。完整条目见 [CHANGELOG](CHANGELOG.md)。

### LB 安装、业务入口与动态管理

02 自动安装 HAProxy、Keepalived、psmisc 和 Python，配置主备优先级、共享八字符 VRRP 认证、root 所有的健康检查程序及脚本安全设置；校验候选配置后写入，并检查运行状态和开机自启动。相同配置重跑不重启已运行服务，失败时恢复配置及相关服务状态。

部署时可额外填写应用入口 VIP，例如 `10.20.1.20`。API VIP 继续负责 Kubernetes API（默认 `10.20.1.9:6443`），应用 VIP 供业务入口使用。安装器持久化 `ip_nonlocal_bind=1`，使备用节点能预先监听业务 VIP；两台 LB 完成后检查实际 VIP 单一归属。两项 VIP 当前共享一个 VRRP 实例，会一起切换。

```text
管理客户端 → API VIP:6443 → HAProxy → Kubernetes 控制平面
业务客户端 → 应用 VIP:443 → HAProxy → Gateway/Ingress NodePort → Service → Pod
```

第二条路径需要先安装 Gateway/Ingress、读取真实 Service NodePort，再用 05 创建转发规则。02 不固定某个 NodePort，也不自动生成业务 DNS、证书或上游 ACL。

05 提供每 3 秒刷新的状态视图，显示两台 LB 的服务、配置校验、地址、前后端健康和连接计数；支持业务 VIP 和 HTTP/TCP 规则的增删查。HTTP 转发可延长 WebSocket tunnel timeout，但节点故障后既有连接仍可能断开，应用需要重连机制。

| 05 管理对象 | 本版能力 |
| --- | --- |
| 自有 LB | 登记/移除目标、设置 SSH 用户；不自动创建或初始化新服务器 |
| 业务 VIP | 管理标记内的 VIP，保护现有 API VIP |
| HAProxy 规则 | 按端口及指定 VIP/通配地址转发到多个 `IPv4:PORT` 后端，HTTP/TCP 模式 |
| 配置变更 | 所有目标候选校验、备份、逐节点 reload、失败时尝试回滚 |
| 外部 LB | 独立清单登记；尚未对接设备或云厂商管理 API |

本版 05 为试用功能，尚不提供同端口域名/路径规则、TLS 终止、证书管理或多集群灾备编排。API 和应用入口可以共用这对 LB，也可以按故障隔离要求另建独立 LB。详见 [LB 管理说明](docs/03-负载均衡与业务入口/01-LB运维管理指南.md)。

### 审计、旧集群接管与存储保护

02 增加 `--check` 与 `--repair`，方便已运行集群核对当前主机基线。检查覆盖身份唯一性、SSH/sudo、hosts、运行时、LB 服务和实际存储路径；旧节点可通过配对码补充审计访问。修复支持逐项预览、确认、备份和修复后复查，详细命令见下方[旧集群修复](#修复已运行的旧集群)。

01/02 与初始化、加入阶段统一使用存储检测规则，识别分区、LVM、RAID 及组件当前数据目录。已有组件数据不会因默认选盘而被新挂载遮盖；对其他独立挂载空间，确认后仅使用专属子目录，保留现有文件系统和文件。

### 插件、下载源与兼容性

| 编号 | 插件 | 用途 |
| --- | --- | --- |
| 1 | Headlamp | Kubernetes 可视化控制台，建议先安装 |
| 2 | Metrics Server | CPU/内存指标、kubectl top、HPA |
| 3 | cert-manager | 应用 TLS 证书管理 |
| 4 | Prometheus stack | 指标、告警、Grafana |
| 5 | Loki + Alloy | 日志存储与采集 |
| 6 | Argo CD | GitOps 应用交付 |
| 7 | External Secrets | 外部密钥同步 |
| 8 | ExternalDNS | DNS 记录同步 |
| 9 | Velero | Kubernetes 资源及持久卷备份 |
| 10 | NFS CSI | 基于现有 NFS 动态供应卷 |
| 11 | Envoy Gateway | Gateway API 应用路由 |
| 12 | Kyverno | 资源策略 |
| 13 | MetalLB | 裸机 LoadBalancer 地址通告 |
| 14 | KEDA | 事件驱动扩缩容 |
| 15 | Traefik Ingress | Ingress 请求路由 |
| 16 | Longhorn | 节点磁盘构建副本块存储 |

Headlamp 默认使用 ClusterIP 和只读权限。安装器启动及后续插件安装/升级成功后，会自动为已存在的 Headlamp 同步相关实际 CRD 的只读权限，减少逐项手补 RBAC；不因此授予写操作或 Secret 读取权限。先装其他插件也可在后装 Headlamp 时补齐。

Longhorn 安装前自动通过节点互信安装 iSCSI/NFS 等主机依赖，避开不兼容的 open-iscsi 2.1.12，并解释副本数对冗余和容量的影响。Longhorn 使用节点本地磁盘，不能替代异地备份；磁盘容量、文件系统和 Base Image 的挂载传播要求仍需确认。

全部插件共用下载源选择，覆盖 Helm HTTP/OCI Chart 和已识别的安装镜像。源选择不会自动修改已有 release，明确升级后才应用新配置；未知私有仓库保持原地址，公共代理不保证可达。版本选择结合实际 Kubernetes 版本、Chart 声明和已适配的官方兼容表，并进行预渲染检查。Envoy Proxy 镜像随所选版本自动解析，控制器就绪后可选创建指向现有 Service 的基础 HTTP Gateway/HTTPRoute。

### 语言目录与后续扩展

`locales/en.json`、`zh_cn.json`、`zh_tw.json` 保存文本，脚本通过 messageKey 读取。英文为基础回退，繁体中文可回退到简体中文、再到英文；支持自动发现语言目录和共享 Bash/Python 接口。

```bash
sudo bash ./01-prepare-node.sh --lang zh_CN
sudo bash ./02-deploy-cluster.sh --lang zh_TW
sudo bash ./03-install-addons.sh --lang en
sudo bash ./04-verify-cluster.sh --lang zh_CN
```

增加语言时新建一个 JSON，填写 `locale`、`displayName`、`fallback` 与同名 messageKey 的翻译，保留格式占位符。主要交互已迁移；02 的部分详细诊断、审计/修复程序及内部阶段仍待迁移，第三方命令输出保留原样。架构和迁移清单见 [国际化说明](docs/08-项目维护与发布/01-国际化与文本维护.md)。

## 0.3.0 新特性

0.3.0 相对 v0.2.0 新增六类持续运维能力，重点是统一配置和变更流程、备份与恢复准备、节点生命周期管理及离线交付。

| 运维任务 | 本版提供的选项 | 使用边界 |
| --- | --- | --- |
| 安全交接 | 检查临时授权与配对服务；独立新通道验证保留账号后执行收尾，失败回退 | 只删除工具自己的授权，不改管理员自建规则 |
| 备份与恢复 | age 加密配置/etcd、完整性校验、恢复计划、隔离目录恢复和实际 etcd 启动演练 | 生产原地恢复、卷和应用数据库恢复须独立方案 |
| 配置与变更 | JSON 模板/导入导出、revision、漂移、SHA 计划、预览/确认/执行/继续及 CAS 锁 | 仅接受已实现的专属操作，不自动应用任意配置差异 |
| 节点与组件 | worker 加入/维护/退出/替换，Kubernetes/Cilium 同 minor 补丁升级，PDB 与存储保护 | 不自动变更控制面成员；失败停止，不盲目降级 |
| 报告与保护证据 | JSON/文本、9 类白名单诊断包，卷/应用的外部加密备份证据登记 | 密文 SHA 和资源 UID 匹配不代表应用恢复通过 |
| 锁定交付物 | 完整依赖闭包、签名离线 APT、镜像 digest、Chart/工具/源码 SHA，企业公开 CA 配置 | 首批 Ubuntu 24.04 amd64 基座；插件/业务镜像及认证凭据独立管理 |

```bash
bash ./06-maintain-cluster.sh --lang zh_CN
bash ./06-maintain-cluster.sh --lang zh_CN --config-template \
  --export-file "$HOME/cluster.example.json"
bash ./06-maintain-cluster.sh --lang zh_CN --report \
  --target ubuntu@cp1.example --sudo --output-dir "$HOME/atlas-reports"
```

菜单与 CLI 共用执行契约。变更先生成绑定集群身份、有效期和当前事实的计划，再用完整计划 ID
确认执行。中断保留状态及锁，确认旧作业停止后显式恢复；不通过超时自动抢占。
02 首次/已完成部署、02 审计及 04 选中检查生成统一摘要；原始特权输出不会复制进常规报告。
05 的事务和主机预留在重启后仍存在，回退遇到第三方修改会停止。

新装默认组件由 `scripts/lib/component-versions.json` 统一定义：Kubernetes v1.36.6、
Cilium 1.20.4、Helm v3.18.6、cri-tools v1.36.0；更换工具源码不会自动升级已有集群。
详细步骤见 [06 操作指南](docs/04-持续运维/04-持续运维操作指南.md)、[统一配置](docs/01-集群部署与配置/02-统一集群配置.md)、
[离线与企业仓库](docs/06-离线交付/01-锁定物料与离线部署.md)及[数据保护](docs/05-数据保护/01-卷与应用数据保护登记.md)。

## 0.4.0 候选发布：监控与告警

当前正式版本仍为 `VERSION` 中的 0.3.0。0.4.0 已完成候选发布验收，尚未创建正式提交、tag 或 GitHub Release。部署时由 02 明确登记集群用途，03 按用途执行监控中心或应用集群采集端部署。

- `monitoring` 集群运行集中式规则、Alertmanager、Grafana、Loki 和通知组件；`application` 集群根据监控接入需要部署 Agent，使用唯一 `cluster` 身份，避免不同集群的同名对象混淆。
- 监控中心与应用集群作为不同 Kubernetes 部署目标，可让多套应用集群接入同一中心。监控中心仍依赖其 Kubernetes 集群；生产环境需根据可用性要求单独规划该集群的节点、备份和故障域。
- 独立观察器运行在被监控集群之外，负责 API VIP、DNS 和监控链路等外部可达性探测。与被监控系统共用物理主机仍存在共同故障域，覆盖报告会保留此边界。
- 候选回归 572 项通过、1 项环境限制跳过；真实隔离集群验证 LB/VIP 故障恢复、Longhorn 副本恢复与数据校验，以及外部观察器通知。版本发布状态详见 [变更记录](CHANGELOG.md)。

- Prometheus 采集与规则覆盖节点、控制面、etcd、CoreDNS、Deployment、StatefulSet、DaemonSet、Pod 启动/重启/OOM、Job、HPA 指标状态、KEDA scaler/ScaledObject 错误与延迟及 Operator 目标失联、HAProxy 和 Longhorn；隔离环境已验证 Pending 告警、HAProxy 后端、Longhorn 磁盘与通知中继失败告警触发和恢复。KEDA 2.21.0 E2E 验证 scaler 错误及 Operator 目标缺失的触发/恢复；Prometheus/Alertmanager 默认自监控规则加载健康，Alertmanager 目标缺失告警经 ServiceMonitor 移除/恢复验证触发与解除。
- kubelet/containerd 状态由 02 安装的主机 systemd 采集器写入 textfile，03 配置 node-exporter 只读采集。告警规则使用文件最后写入时间发现采集器停更。已有集群可用 06 的 `monitoring-systemd-collection` 分步安装并续作，也可以用 `monitoring-systemd-remove --node <节点名>` 卸载单节点采集器。隔离三节点已验证真实采集、kubelet 故障告警及恢复，并在 cp1 验证卸载、计划重放及采集恢复。03 首次交互安装已在一节点和三节点 Kind 集群通过，控制面指标接线的验收边界见[监控设计](docs/07-监控与预警/03-监控与告警功能设计.md)。
- 06 号入口用计划、确认和集群锁管理规则、受管邮件及机器人通知；`monitoring-lb` 将 HAProxy 访问范围、指标采集和 LB 告警整合成可安全续作的单一变更。告警邮件采用简体中文 HTML 模板，显示触发/恢复状态、摘要、处理建议和对象信息。
- 06 逐风险覆盖审计可核对登记的 Prometheus 抓取目标和原始样本时间，并读取本机独立观察器的健康证据。规则运行和通知送达有各自的验收要求。
- 03 的监控首装可为 Prometheus、Alertmanager 和 Grafana 分别选择持久卷容量。隔离测试已验证 Pod 重建后保留历史样本、静默和 Grafana 目录，也验证了实际 Longhorn 卷上的 Prometheus 跨节点重建。
- 06 的 `monitoring-node-network-rules` 可为指定网卡配置带宽和丢包告警，并按节点监控 TCP 重传和监听队列丢弃。可登记实际链路容量，单独发现缺失或过期的采集数据。 [登记方式与阈值](docs/07-监控与预警/03-监控与告警功能设计.md#节点网卡与-tcp-质量)
- 独立观察服务可作为用户级 systemd 服务运行，持续探测业务 HTTP/HTTPS 或 DNS，并通过企微 Webhook 报告故障和恢复；systemd watchdog 可检测观察进程卡死并自动重启，独立 `OnFailure` 服务会尝试发送中文进程故障通知。通知失败可在用户级 systemd 日志中查看。该保护不覆盖观察主机断电或与集群共用宿主机的故障。
- 03 安装 Blackbox Exporter 后，06 可登记集群内 Service DNS、ClusterIP 或 Pod IP，由每个节点执行 HTTP 探测并创建中文告警；出口网络策略限制为登记目标和集群 DNS。10.20.1.28–.30 三节点 E2E 验证探测成功、故障告警触发和策略恢复。
- 日志栈自监控随 03 的 Prometheus 或日志菜单自动同步。Prometheus 采集默认 Loki 和 Alloy 的写入量、重试与丢弃指标，并告警采集失联、Loki 拒收或 Alloy 投递丢弃。
- 诊断闭环：03 菜单 19 提供一键启动的引导序列，依次执行 Prometheus、Loki/Alloy 和通知配置 QA；既有 release 会检查并复用，等待关键组件 Ready，后续失败时保留已完成阶段供续跑。各子阶段保留自身 QA 和 06 计划确认，不是一个原子变更或自动回滚事务。菜单 5 也可单独安装 Loki/Alloy；默认 Loki 使用持久卷和单副本，生产高可用需外部对象存储及审核过的 values。通知中继代码由脚本打成确定性 zipapp，写入内容哈希命名的不可变 ConfigMap；使用已锁定摘要的 Python 运行镜像，并按安装时选定的镜像源自动改写，可选覆盖为企业仓库中的摘要固定镜像，无需用户预先构建中继镜像。菜单 18/19 可逐条配置邮件、企业微信、钉钉和飞书通知路由，凭据用隐藏输入经标准输入创建 Kubernetes Secret，复用 06 变更计划预览和完整计划 ID 确认；机器人地址限制为各自官方 HTTPS endpoint。只有配置格式合法的公网 Grafana HTTPS Explore 地址才会加入链接；未配置时通知附有界的中文诊断日志。03 向导不替用户创建公网域名、Ingress/Gateway、TLS Secret 或访问控制，须由环境管理员配置并单独验收。
- 隔离多节点集群已验证 Longhorn 副本故障、告警、重建、恢复和数据一致性；双节点 Cilium E2E 验证 Longhorn 指标端口只允许授权 Prometheus 来源，并实际拦截无关 Pod；独立观察服务在 LB 上验证了 API VIP 入口故障与恢复。

已知边界：业务 SLO、数据库/缓存/消息队列和实际业务入口须逐集群登记；备份新鲜度/恢复验证属于 0.5.0；Gateway/Ingress 专项适配属于后续规划；测试用虚拟机共用 Proxmox 宿主机，尚未验证跨物理故障域失联。生产部署前须按覆盖报告检查这些项目，不得把未配置项当作健康。邮件触发与恢复已由用户确认在 QQ 客户端显示为中文卡片；企微、钉钉、飞书测试群的告警触发与恢复消息均已确认收到，钉钉和飞书通知自动附加 `atlas` 关键词。完整状态和范围见[监控设计](docs/07-监控与预警/03-监控与告警功能设计.md)及[变更记录](CHANGELOG.md)。

## 部署流程

```mermaid
flowchart TD
    A[安装 Ubuntu Server 24.04] --> B[每台节点运行 01：检查与初始化]
    B --> C[重启后 systemd 续跑并发布 node.conf / 配对码]
    C --> D[任意已准备节点运行 02：登记与互信]
    D --> E[按角色配置 LB / 初始化 cp1 / 加入其余节点]
    E --> F[基础集群检查通过]
    F --> G[有 Helm 和管理员 kubeconfig 的 CP 运行 03：按需安装插件]
    F --> H[各节点运行 04：按角色检查与验证]
    G --> H
    F --> I[管理机运行 05：LB 实时状态 / 业务 VIP / 转发规则]
```

逐步配置项、故障恢复和完整流程图见 [部署与运维指南](docs/01-集群部署与配置/01-集群部署与运维指南.md)。

## 运行要求

| 项目 | 要求 |
|---|---|
| 系统 | 部署目标为 Ubuntu Server 24.04、amd64、Bash 4+、Python 3；macOS 系统自带 Bash 3.2 不满足交互脚本要求 |
| 脚本包 | 保留完整仓库目录，尤其是 `locales/`、`scripts/lib/`、`scripts/stages/` 和 `scripts/lb/`，不要只传入口文件 |
| 资源 | 每台至少 2 GiB 内存、系统盘至少 10 GiB 可用；CP 至少 2 vCPU。插件额外消耗需单独评估 |
| 节点命名 | `lb1/lb2/...`、`cp1/cp2/...`、`w1/w2/...`；至少包含 `lb1` 和 `cp1`，worker 可选 |
| 网络 | 每台唯一且可达的内网 IP；节点互通；当前 HAProxy/Keepalived VIP 方案要求 LB 支持同二层 VRRP/ARP |
| 管理账号 | 全节点使用相同的 SSH 管理员用户名；有 sudo 权限及控制台恢复通道 |
| 依赖来源 | 默认访问 Ubuntu/Kubernetes 包源、容器镜像与 Helm 仓库；也可预先构建同平台基座离线物料，插件和业务镜像另行准备 |
| 高可用拓扑 | HA 测试示例为 2 LB + 3 CP + 3 worker；单 LB 或单 CP 部署不具备对应层高可用能力 |

云服务器可选择 `keep` 网络模式保留现有 IP。**保留 IP 不代表云网络支持当前 VIP 方案**：若云平台不允许 VRRP/ARP 漂移，需要先设计相应 API 入口方案；本工具不自动适配云负载均衡。

01 会变更主机配置、重启并禁用 SSH 密码登录；应先准备管理员公钥和控制台访问。节点间免密 sudo 与 SSH 互信的权限范围、数据盘操作和收尾要求见 [安全说明](SECURITY.md)。

## 快速开始

先通过 GitHub 下载完整仓库或源码压缩包并解压。在下载目录操作；下例的 IP 是示例，必须替换为实际节点 IP。

### 1. 逐台准备节点

将完整项目复制到每台服务器，保留相对目录。以下命令在本机仓库根目录执行：

```bash
NODE_IP=10.20.1.20
ssh "ubuntu@$NODE_IP" 'mkdir -p ~/deploy'
scp -r ./. "ubuntu@$NODE_IP:~/deploy/"
```

登录目标服务器后执行；只有控制台可访问时，通过平台支持的文件传输方式放入脚本：

```bash
cd ~/deploy
sudo bash ./01-prepare-node.sh
```

首次运行先选择界面语言，再按提示选择节点名、网络模式、管理员公钥等。也可用 `--lang en`、`--lang zh_CN` 或 `--lang zh_TW` 指定语言。完成重启和自动续跑后检查：

```bash
sudo bash ./01-prepare-node.sh --status
sudo bash ./01-prepare-node.sh --code
cat ~/.k8s-deploy/node.conf
```

其他节点也完成阶段一后再开始 02。`--config-only` 只补生成已完成节点的配置，不替代正常准备流程。

### 2. 在一个集群节点集中部署

发起节点需要完整脚本包。其他节点完成准备后，在其中一个节点的项目目录执行：

```bash
cd ~/deploy
sudo bash ./02-deploy-cluster.sh
```

逐个输入其他节点配对码，输入 `done` 结束登记；核对拓扑、VIP、网段和数据盘计划后确认部署。数量不限于示例 8 台。脚本会分发内部脚本并按角色执行。

出错时先看日志、修复原因，再从保存部署计划的原发起节点运行同一命令。不要直接删除状态目录或执行 `kubeadm reset`。

已有集群可先从可 SSH 访问的节点运行只读检查。显式传入**完整**清单；命令在发起节点执行，需能以管理员私钥 SSH 登录所有节点：

```bash
sudo bash ./02-deploy-cluster.sh --check \
  --user ubuntu --identity /home/ubuntu/.ssh/id_ed25519 \
  --node lb1=10.20.1.20 --node lb2=10.20.1.21 \
  --node cp1=10.20.1.22 --node cp2=10.20.1.23 --node cp3=10.20.1.24 \
  --node w1=10.20.1.25 --node w2=10.20.1.26 --node w3=10.20.1.27
```

旧集群没有节点私钥或互信时，先复用配对码建立审计访问。在其他节点的 SSH 终端或控制台运行以下命令；它使用当前 hostname、IP 和 SSH 账号，不进入系统初始化、网络或数据盘交互，也不生成阶段一完成标记：

```bash
sudo bash ./01-prepare-node.sh --pair
```

在发起节点运行 `sudo bash ./02-deploy-cluster.sh --check --pair`，一次粘贴其他节点的配对码，每行一个，最后输入 `done`。脚本解析码中的 IP，通过与阶段二部署相同的协议追加审计公钥授权、验证 SSH 主机公钥，并自动登记本机。专用审计私钥仅留在发起节点，节点清单和经过认证的主机公钥保存到登录账号的 `~/.k8s-deploy/audit-nodes.conf` 和 `audit-known_hosts`。后续直接运行 `--check` 即可；`--check --sudo` 会询问一次远程 sudo 密码，用于读取需要 root 的设置，不保存密码或修改 sudoers。

省略 `--node` 时，优先复用已保存的审计清单，其次尝试本机 `/etc/hosts`；没有可用清单时显示一次输入提示，可以整块粘贴 `NAME=IPv4`、`NAME IPv4`、`NAME:IPv4` 或配对码，以空行或 `done` 结束。`--check --pair` 可强制重新登记。只有明确输入配对码时才建立新的 SSH 授权；普通 `--check` 读取配置并报告问题，本机直接采集，缺少远程身份时明确标记未验证。缺失的显式 `--identity` 路径会报错。报告不会用已有部署状态标记代替实时检查，会区分失败与未验证，并逐项列出建议处理方式。

#### 修复已运行的旧集群

从保存审计清单的集群节点运行，先预览全部修复项目：

```bash
cd ~/deploy
sudo bash ./02-deploy-cluster.sh --repair --sudo --items all --dry-run
```

应用时进入菜单选择，或使用 `--items all` 选全部六项：

```bash
sudo bash ./02-deploy-cluster.sh --repair --sudo
# 或：已核对预览计划后选择全部六项
sudo bash ./02-deploy-cluster.sh --repair --sudo --items all
```

| 菜单 | 修复项目 | 行为 |
| --- | --- | --- |
| 1 | `hosts` | 修复全节点的 hostname/内网 IP 映射，保留无关别名 |
| 2 | `node-keys` | 补齐独立节点密钥、分发公钥、配置并验证 SSH 互信；保留管理员授权 |
| 3 | `ssh` | 验证管理员访问后配置密钥登录和禁止 root 登录；新连接失败恢复原策略 |
| 4 | `sudo` | 为指定 SSH 用户配置 root `NOPASSWD:ALL`；这项会扩大无密码自动化权限 |
| 5 | `adopt` | 验证旧节点基线，登记真实 node.conf 并写入验证来源，最后标记 ready |
| 6 | `host-keys` | 轮换重复的 SSH 主机密钥、更新集群 known_hosts，并显示新指纹 |

默认菜单选择 `1,2,3,4,5`，不包含主机密钥轮换；你的报告若有重复主机密钥，旧节点注册将保持阻止状态，需明确选择第 6 项或 `all`。也可用 `--items hosts,node-keys` 只修部分项目。实际修改要求终端输入 `REPAIR`；第 6 项额外要求输入 `ROTATE HOST KEYS`。

每台节点的原文件保存在 root-only 的 `/var/lib/k8s-deploy/repair-backups/`。完成项保留，错误时停止，解决原因后可以重跑。修复不会重新安装 Kubernetes、改网络/机器 ID、重启节点或更换数据挂载。`adopt` 要求唯一身份、可用互信、SSH/sudo 基线、运行时和存储全部满足；不足时不会新建 ready 来掩盖问题。已有正确注册配置会保留。

**选择主机密钥轮换后，Mac/DBX 等外部客户端可能报告指纹变化。** 核对控制台或脚本输出的新指纹后，定向更新客户端的 known_hosts；集群节点无法替你修改管理员电脑上的记录。修复前保留当前会话及控制台入口。

详见 [旧集群修复流程与安全边界](docs/01-集群部署与配置/01-集群部署与运维指南.md#02旧集群基线修复) 和 [修复功能设计](docs/01-集群部署与配置/03-旧集群基线修复功能设计.md)。

### 3. 按需安装插件

在具有 Helm 和管理员 kubeconfig 的控制平面节点（阶段二默认是 cp1），进入完整项目目录：

```bash
cd ~/deploy
sudo bash ./03-install-addons.sh
# 或
sudo bash ./03-install-addons.sh --lang zh_CN
```

启动后依次**选择语言 → 选择下载源 → 进入插件主菜单**。指定 `--lang en/zh` 时跳过语言选择，仍会先展示下载源菜单。下载源菜单显示已保存的选择，回车或输入 `0` 保留并继续；首次默认为官方源。之后选择插件，查看二级说明、确认安装并输入插件专属配置。主菜单 `0` 退出，`L` 切换语言，`S` 随时更换下载源。当前源供全部 16 项插件的后续安装/升级共用，并保存在登录账号的 `~/.k8s-deploy/addon-sources.json`。

可选官方源（默认）、DaoCloud、1Panel + DaoCloud、Google Docker Hub 缓存和自定义企业代理。国内可先尝试 DaoCloud；公共源可能缺包或限流，不能保证当前网络可达。1Panel 方案明确组合 DaoCloud 代理其他仓库；Google 只缓存部分 Docker Hub 内容，不是全插件镜像站。

普通 Helm 索引/Chart 包和 OCI Chart 统一进入下载策略；全部插件使用所选 Chart 的配套镜像，安装时处理常见公开仓库镜像、Helm 钩子镜像配置及 Envoy 数据面配置。Envoy Proxy 镜像根据所选版本的官方发布建议、Chart 默认值或对应版本源码自动确定，不再要求输入镜像坐标；额外 values 仍可覆盖。所选源失败不会静默切回官方。现有 release 不会仅因切源就被修改，需单独确认升级；未知私有仓库、后续业务镜像和运行时配置保持独立。所有插件的版本及镜像处理需要 `python3-yaml`，缺少时会询问是否通过 Ubuntu apt 安装。03 需要与 `locales/` 和 `scripts/lib/` 一起复制，才能使用语言菜单和 messageKey。

Headlamp 排在插件菜单首位，建议先安装。它默认只通过 ClusterIP 提供服务，并绑定 Kubernetes `view` 只读角色；访问前请配置身份认证方式或使用 `kubectl port-forward`。安装器会在启动时及每个插件成功安装/升级后，检查 Headlamp 是否存在，并按已部署插件实际注册的 CRD 资源自动同步 `get/list/watch` 只读授权，不授予写权限或读取 Secret。Longhorn 安装前会通过阶段二的 SSH mesh 在全部 Kubernetes 节点自动安装并启用 `open-iscsi`/`iscsid`、加载并持久化 `iscsi_tcp`，并补齐主机工具和 NFSv4 客户端；脚本会避开 Longhorn 标记不兼容的 open-iscsi 2.1.12。Longhorn Base Image 的挂载传播、磁盘容量和文件系统适用性仍需按实际节点确认。如果已有默认 StorageClass，03 不会自动替换。安装 Envoy Gateway 时，控制器就绪后可选创建基础 HTTP Gateway/HTTPRoute 指向现有 Service；TLS、DNS、认证与可达的外部负载均衡仍需按环境配置。

**全部插件统一按实际 Kubernetes 版本筛选版本**：读取服务端 `/version`，按 Chart 的 `kubeVersion` 和已适配的官方兼容表推荐稳定候选，Envoy 也不再固定默认版本。手动选择后重新检查实际下载包，并按当前集群版本/API 预渲染。明确不匹配时停止；缺少兼容依据时标记 `UNKNOWN` 并默认拒绝继续。`CHART` 只表示符合 Chart 声明，不代表完整上游测试；额外 values 的自定义镜像仍须单独核对。详细范围见[版本与 Kubernetes 匹配](docs/02-插件管理/01-插件下载源与兼容性.md#全部插件的版本与-kubernetes-匹配)。

来源地址、覆盖范围、自定义代理的路径约定及排错见 [插件下载源](docs/02-插件管理/01-插件下载源与兼容性.md)。

### 4. 按角色检查

在需要检查的节点进入完整项目目录：

```bash
cd ~/deploy
sudo bash ./04-verify-cluster.sh
# 或
sudo bash ./04-verify-cluster.sh --lang zh_CN
```

LB 不展示 kubectl 集群菜单，worker 展示本机运行时/CNI/API 连通性，CP 在具备 kubectl 和 `admin.conf` 时展示全局检查。节点 Ready、插件 Pod Running 与业务功能、备份恢复通过是不同结论。

### 5. 管理现有 LB

在能通过 SSH 登录所有 LB 的管理机上，以持有 SSH 私钥的普通账号运行：

```bash
cd ~/deploy
bash ./05-manage-lb.sh
```

首次进入“自有 LB 节点清单”，设置 SSH 用户并登记实际节点，例如 `lb1=10.20.1.12 lb2=10.20.1.19`。目标节点必须已安装并配置 LB、支持免密 SSH 和 `sudo -n`。选择“实时状态”核对服务与配置，再登记业务 VIP、创建转发规则。

本机清单位于 `~/.config/atlas-kubekit/lb-manager.json`；远端受管业务状态位于 `/etc/atlas-kubekit/lb-manager.json`。两者用途不同。API VIP 不通过业务 VIP 菜单删除。完整操作及回退边界见 [LB 运维说明](docs/03-负载均衡与业务入口/01-LB运维管理指南.md)。

## 升级现有环境

1. 备份 `/var/lib/k8s-deploy/` 中的部署状态、节点清单和相关 LB 配置；若存在自动续跑任务，先确认它的状态。不要在续跑中途只替换部分文件。
2. 将完整 `0.3.0` 项目放入新的目录，查询 `--version`。旧的服务器安装副本不会随仓库自动更新。
3. 旧集群先执行 `02 --check`，需要修复时先看 `--repair --dry-run` 计划，再选择具体项目。版本升级本身不执行 kubeadm、不升级 Kubernetes 或插件，也不迁移数据。
4. LB 安装脚本重跑保留受管业务路由/VIP；状态或标记损坏时会停止，应先核对备份。生产环境仍需安排可接受短暂切换的维护窗口。
5. 插件按名称选择：Headlamp 插入首位后编号改变。已有 Helm release 默认跳过，明确选择升级才应用新版本/下载源。
6. 使用 06 管理已有集群时，配置填写当前实际版本和节点，不直接采用新装模板默认值。先巡检、核对备份，再预览并确认专属变更计划；不为升级重跑 01/02。具体见 [持续运维指南](docs/04-持续运维/04-持续运维操作指南.md)。

与 `0.1.0` 相比，正常交互需要外置语言目录和共享运行库，旧的单文件传输方式必须调整。`0.x` 阶段的行为变化以 [CHANGELOG](CHANGELOG.md) 为准，不视为稳定版兼容承诺。

## 查看工具版本

工具版本与 Kubernetes、Cilium 等组件版本相互独立。使用入口脚本查看当前脚本版本：

```bash
bash ./01-prepare-node.sh --version
```

版本号不是部署成功标记；源码更新后，应确认目标机器实际运行的脚本副本。

## 入口脚本

| 脚本 | 用途 |
| --- | --- |
| `01-prepare-node.sh` | 准备单台 Ubuntu 节点并建立安全基线 |
| `02-deploy-cluster.sh` | 登记节点并部署 Kubernetes 集群 |
| `03-install-addons.sh` | 选择并管理可选插件 |
| `04-verify-cluster.sh` | 按节点角色检查集群状态 |
| `05-manage-lb.sh` | 管理 HAProxy、Keepalived、业务 VIP 和转发规则 |
| `06-maintain-cluster.sh` | 管理配置、备份、节点生命周期和受控升级 |

脚本通过 SSH 和 Kubernetes API 对接节点及集群。请复制完整项目目录，以包含运行所需的语言目录和辅助文件；详细步骤见[部署指南](docs/01-集群部署与配置/01-集群部署与运维指南.md)。

## 用户文档

从 [产品概览与架构](docs/00-产品概览与架构.md) 了解组件与整体设计，再按需要阅读 [文档导航](docs/00-文档导航.md) 中的部署和运维指南。

- [部署指南](docs/01-集群部署与配置/01-集群部署与运维指南.md)：从节点准备到插件安装和日常检查。
- [统一集群配置](docs/01-集群部署与配置/02-统一集群配置.md)：配置模板、导入导出和环境差异检查。
- [插件说明](docs/02-插件管理/01-插件下载源与兼容性.md)：可选组件用途、下载源和兼容性。
- [LB 运维管理](docs/03-负载均衡与业务入口/01-LB运维管理指南.md)：VIP、HAProxy 转发和状态查看。
- [持续运维指南](docs/04-持续运维/04-持续运维操作指南.md)：计划式变更、备份、恢复准备和诊断。
- [数据保护范围](docs/05-数据保护/01-卷与应用数据保护登记.md)：控制面、卷和应用数据的保护边界。
- [离线交付](docs/06-离线交付/01-锁定物料与离线部署.md)：锁定依赖和受限网络部署。
- [监控与预警规划](docs/07-监控与预警/01-生产监控与预警功能定义.md)：目标、验收边界和风险覆盖范围。
- [监控与告警设计](docs/07-监控与预警/03-监控与告警功能设计.md)：当前开发范围、部署方式、已验证能力和已知限制。
- [产品版本路线图](docs/09-产品路线图/01-版本里程碑与能力规划.md)：0.4.0 监控告警及后续迈向 1.0.0 的阶段目标。
- [版本变更](CHANGELOG.md)：面向使用者的新增能力、兼容性变化和已知限制。

## 贡献与问题反馈

普通问题和建议通过仓库 Issues 提交，改动通过 Pull Request 提交。请先阅读 [贡献指南](CONTRIBUTING.md) 与 [行为准则](CODE_OF_CONDUCT.md)。安全漏洞通过 [安全说明](SECURITY.md) 指定的私密渠道报告，避免公开凭据和可利用细节。

## 许可证

项目原创脚本与文档采用 [MIT License](LICENSE)，版权署名为 `Atlas Richie`。完整许可文本见 LICENSE，标准文本来源为 [Open Source Initiative](https://opensource.org/license/mit)。通过脚本安装的第三方软件、镜像和 Chart 各自遵循上游许可证，不因本项目而变为 MIT。
