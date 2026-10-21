# Atlas KubeKit — 变更记录

本文件记录面向使用者的功能、兼容性与已知限制。工具集版本以 [VERSION](VERSION) 为准，版本递增与发布方式见 [版本管理指南](docs/versioning.md)。

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

### 验证与已知限制

- 新增 13 项 LB/编排回归与 2 项主机续跑回归，覆盖重跑、权限、故障回退、VIP 归属、认证复用、join 参数及语言依赖；记录见 [安装回归](docs/validation/lb-installer-regression.md)。
- 维护者于 2026-10-22 确认 0.2.0 脚本已在本地 Kubernetes 集群完成完整验证，并批准创建 tag 和正式 Release；确认来源与范围见 [发布验收记录](docs/validation/release-0.2.0.md)。
- 已在两台 Ubuntu 24.04.5 克隆节点执行真实 LB 安装、业务 TCP 路由、重跑、服务故障切换与两台重启验证；API VIP 查询得到六节点 Ready。HAProxy 停止到切换约 5 秒，存在短暂请求失败，见 [真实双机 E2E](docs/validation/lb-live-e2e-20261022.md)。历史记录保留原始源码 hash、日期及范围。
- 本地集群验收不代表所有云网络、部署拓扑、容量和跨地域灾备场景已验证；具体环境条件及现有功能限制继续适用。
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
