# Atlas KubeKit — 变更记录

本文件记录面向使用者的功能、兼容性与已知限制。工具集版本以 [VERSION](VERSION) 为准，版本递增与发布方式见 [版本管理指南](docs/versioning.md)。

## Unreleased

后续尚未纳入发布版本的变更记录在此，发布时移入对应版本条目。

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
- 根目录保留 01～04 入口，8 个内部部署脚本集中在 `scripts/stages/`；完整指南、版本流程及历史验证摘要放入 `docs/`。
- 提供 MIT 许可证、贡献与安全说明、行为准则、Issue/PR 模板、Git 忽略规则及文件格式配置。

### 兼容性与使用说明

- 这是首个版本，没有上一发布版本可比较；`0.x` 阶段的接口和部署行为仍可能调整，后续变化会在对应版本说明中记录。
- 02 优先读取新仓库的 `scripts/stages/`，同时兼容旧平铺包和 `/usr/local/lib/k8s-deploy/` 安装目录。
- 保留 `/var/lib/k8s-deploy/`、`~/.k8s-deploy/` 及已有 systemd 服务名，不因项目命名变化而迁移部署状态。
- 02 发起节点必须持有完整内部脚本包；01、03、04 可单独复制。源码更新不会自动覆盖服务器副本。
- 重跑要求先处理具体失败原因；脚本不会自动重置集群，也不保证所有部分安装状态都能自动修复。

### 已知限制与验证状态

- 当前部署目标仅为 Ubuntu Server 24.04 amd64；其他发行版、架构、离线部署及云负载均衡适配不在首版支持范围。
- HAProxy/Keepalived VIP 需要相应二层 VRRP/ARP 网络条件；`keep` 模式保留云 IP 不代表云 VPC 支持 VIP 漂移。
- 全节点当前要求相同 SSH 管理员用户名；管理员 SSH 互信和非交互 sudo 的权限收紧需在部署收尾单独规划。
- 软件包、容器镜像及 Chart 的下载网络需自行提供；插件使用的外部 NFS、对象存储、DNS 与 Git 服务不由脚本统一创建。
- 插件控制器就绪不等于业务配置完成；节点 Ready 或 Pod Running 不代替网络、数据持久化及恢复验收。
- [插件历史验证摘要](docs/validation/addons-e2e.md) 与 [04 历史验证摘要](docs/validation/verify-results.md) 仅覆盖记录中的实验室版本和核心路径，原始日志与快照已清理。
- 不将历史记录作为当前版本所有配置、云厂商、容量、高可用故障与灾备恢复场景的验证证明。
