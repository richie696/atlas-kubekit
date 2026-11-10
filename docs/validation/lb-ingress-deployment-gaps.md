# LB 应用入口：修复、验证与部署期配置缺口

日期：2026-10-21

状态：当前单节点应用路径通过；双 LB 故障切换尚未注入验证。

## 结论

当前集群采用已有的 `lb1/lb2 + HAProxy + Keepalived` 作为应用入口，不额外安装 Cilium L2、MetalLB 或第二个 Ingress/LB 实现。Kubernetes API 继续使用独立的 `10.20.1.9:6443`；应用使用独立业务 VIP，经 HAProxy TCP/TLS passthrough 转发到 Envoy Gateway 的 NodePort。

部署时已能确定、适合自动配置的是应用入口 VIP；Gateway Service 的 NodePort 和应用域名/证书则要等 Gateway 与业务路由建立后才能确定。因此 Phase 2 现在可预留并验证应用 VIP；Gateway 就绪后再通过 `05-manage-lb.sh` 创建转发规则。不能把当时恰好分配到的 NodePort `32018` 固化为新集群默认值。

## 本轮修复和定向验证

- 在 `lb1=10.20.1.12`、`lb2=10.20.1.19` 为现有 Keepalived 集群增加应用 VIP `10.20.1.20`，同时保留 API VIP `10.20.1.9`。
- 在两台 HAProxy 上配置 TCP `*:443` 转发到六个 Kubernetes 节点的 Gateway NodePort `32018`。TCP passthrough 保留客户端 SNI/TLS，不在 LB 终止 TLS。
- 两台 LB 的 HAProxy 与 Keepalived 均为 active；使用 sudo 对两台节点上的配置分别运行 `haproxy -c`、`keepalived -t`，均通过。
- 从 Mac 通过业务 VIP 请求 `https://sentinel-t003.local/healthz`，返回 `{"status":"ok"}`；通过 API VIP 请求 `/readyz` 返回 `ok`。
- 分别直连六个 K8s 节点的 `32018`，均返回 `{"status":"ok"}`。
- 当前 Envoy Gateway Service 是 `NodePort`，实际端口 `32018`，`externalTrafficPolicy=Cluster`；Gateway `Programmed=True`。
- 当前证书 SAN 包含 `sentinel-t003.local`，由 `sentinel-t003-test-ca` 签发；系统默认信任验证失败（curl 错误 60）。显式跳过验证的连通性测试通过。
- 当前主机没有 `sentinel-t003.local` 的系统 DNS 记录；测试通过 curl `--resolve` 显式映射到 `10.20.1.20`。
- 两台 LB 的 UFW 均未安装；443、6443、7000 均由 HAProxy 监听。此检查不代表上游交换机 ACL、路由器 ACL 或云防火墙已开放。
- `7000` 的 HAProxy stats 页面当前绑定 `*:7000` 且没有 HTTP 认证；应限制到管理网段或只经 SSH 隧道访问。当前没有足够的管理网段/访问源信息，故本轮没有擅自改绑定或防火墙。
- 对修改后的 `setup-lb.sh` 在两台真实 LB 执行 `--check-only`，候选 HAProxy/Keepalived 配置均通过语法验证；此检查不重启服务、不更改配置。

本轮未停止 Keepalived 做故障注入，因为 API 与应用 VIP 当前属于同一个 VRRP 实例，停止服务会同时移动 Kubernetes API VIP。故障切换应另列维护窗口/无业务影响的验收，不作为本次 HTTP 功能通过的证据。

## 部署脚本已经补入的配置

`02-deploy-cluster.sh` 增加可选的“应用入口 VIP”交互项：

1. 留空时不改变原有仅 API VIP 部署行为。
2. 填写时检查格式，并拒绝与 API VIP 或任一登记节点 IP 冲突。
3. 将值保存到 root-only `cluster.conf`，传给每台 LB 的 `setup-lb.sh`。
4. `setup-lb.sh` 把业务 VIP 与现有 LB Manager 状态合并，由 Keepalived 管理；新 VIP 配置后，从所有 Kubernetes 节点验证 ARP 所有权指向登记 LB。
5. 重跑部署时保留 `05-manage-lb.sh` 写入的 HAProxy 受管路由、Keepalived 受管 VIP 和管理器状态；如果标记区块不完整、重复或与状态文件不一致，脚本停止而不是覆盖。
6. 不在 LB 安装阶段硬编码 Gateway NodePort。Gateway 创建后，使用 LB Manager 添加 `TCP / 443 → <所有 K8s 节点>:<Gateway HTTPS NodePort>`。当前运行实例使用 `32018`，新集群应读取其真实 Service 值。

## 其他缺失配置 / 需要记录的前置条件

| 项目 | 当前事实 | 需要完成的时间 | 是否写入通用安装脚本 |
|---|---|---|---|
| 业务 VIP 的网络预留 | `10.20.1.20` 的 ARP 在本次运行可达；ARP 检查不能证明 DHCP 地址池/网络管理系统已永久排除该地址 | 部署前由网络管理员确认保留 | 脚本收集并做集群内冲突检查；无法替代 DHCP/网络设备预留 |
| 外部 DNS | 当前无 `sentinel-t003.local` 解析记录；需解析到 `10.20.1.20` | Gateway hostname 与 VIP 确定后 | 不自动写入，除非未来配置 ExternalDNS provider/zone/凭据；当前只记录提示 |
| TLS 信任 | 当前测试证书由测试 CA 签发，系统默认不信任；Gateway 使用 TLS，LB 仅透传 | 业务域名定稿后，为 Gateway 配可信证书，并把根/中间 CA 发布到客户端信任库 | 不放入通用 LB 安装脚本；由 cert-manager/企业 PKI 与客户端信任管理完成 |
| Gateway HTTPS NodePort | 当前动态值 `32018`；六个节点可达 | Envoy Gateway 创建 HTTPS Listener/Service 后 | 不在 Phase 2 固定。部署脚本必须读取实际 Service 后再登记路由 |
| LB 与节点防火墙/ACL | 本机 UFW 未安装，当前路径可访问；没有验证上游 ACL | 部署前检查客户端→VIP TCP/443、LB→节点 NodePort；管理端按需限制 stats 7000 | 通用脚本提示并做连通性检查；不能自动修改未知上游防火墙 |
| HAProxy stats 管理面 | 当前 `*:7000` 无 HTTP 认证，任何可达该端口的网络都可读取状态 | 生产开放前限定管理源网段，或通过 SSH 隧道访问 | 建议安装脚本新增管理 CIDR 参数；未有管理网段信息，不设置猜测值 |
| VRRP/L2 连通 | 当前 `.9` 和 `.20` 可由已登记节点通过 ARP 解析到 LB；本轮没有做服务停止式 failover | 生产启用前的维护窗口验证 lb1/lb2 切换，并确认 API 与业务 VIP 的中断预算 | 脚本继续做部署期 ARP 检查；故障注入不自动执行 |
| 两个 VIP 的切换关联 | API VIP 与应用 VIP 当前在同一个 VRRP 实例；同一 Keepalived/HAProxy 健康状态会一起切换 | 如需应用入口独立于 API 的优先级/故障策略，需另行设计 VRRP 实例与健康检查 | 当前沿用单实例，避免在无 HA 验收下改变已工作架构 |
| 路由健康策略 | 当前 HAProxy 受管规则使用 TCP `check`；业务健康响应另由应用探测验证 | 创建业务路由时确认是否需要更强的 HTTP/TLS 健康检查 | 目前 Manager 支持范围有限，不改变为未验证的复杂探测策略 |

## 为什么不把所有配置塞进 Phase 2

- Phase 2 先部署 K8s 基础集群；当时并不知道应用域名、Gateway Listener、证书来源或最终 Service NodePort。
- 把 `32018` 写死会把当前集群偶然分配值带到新环境；Service 重建后可能失效。
- DNS、证书信任和网络 ACL 分别属于 DNS/PKI/网络管理边界，未提供 provider、zone 和权限时，脚本不能安全推断或改写。
- Cilium L2、MetalLB 与 HAProxy/Keepalived 是不同入口实现路线；同一地址池/通告职责不能重复安装。当前集群入口已通过 HAProxy/Keepalived 验证，后续保持单一路径。

## 验收边界

已通过：候选配置语法、两台 LB 服务状态、六节点 NodePort 到达、应用 VIP HTTPS/TLS passthrough 健康响应、API VIP 健康响应、安装脚本只读检查。

未通过/未执行：TLS CA 默认信任、DNS 自动解析、上游防火墙核验、Keepalived 故障注入、管理器路由健康探测超出 TCP connect check 的场景。

## 2026-10-22 部署配置复核

本节记录新一轮 LB 只读复核，以及后续经用户授权的 HTTP registry 节点拉取修复；不覆盖上面的 2026-10-21 LB live E2E 记录。

### 当前观测

- Kubernetes API 可读，6 个节点目前均为 `Ready`。
- `lb1=10.20.1.12`、`lb2=10.20.1.19`、应用 VIP `10.20.1.20` 的 HAProxy stats 端点从当前管理客户端均返回 HTTP 200，响应约 34 KB。当前脚本实际使用 `bind *:7000` 且无认证，故管理页面不只对 LB 本机开放。
- Sentinel 验收 namespace 的 Console、Collector、PostgreSQL 和 Envoy 数据面 Pod 此前因镜像地址失效而 `ImagePullBackOff`；本节末的 2026-10-22 复测已把验收工作负载切至当前宿主 registry 并恢复服务，具体结果见下文更新。
- 当前 Mac 的 LAN 地址为 `10.20.1.157`。Docker Engine `29.9.2` 与既有 `local-registry` 运行中，宿主 `0.0.0.0:55000` 映射到容器 `5000/tcp`，Docker volume 持久化挂载；本机 `http://10.20.1.157:55000/v2/_catalog` 返回 HTTP 200。六个节点经 host-network probe 均能读取 HTTP `/v2/`；仅在精确 `10.20.1.157:55000` 上追加 containerd `hosts.toml` 后，`imagePullPolicy: Always` 测试镜像在 cp1–cp3/w1–w3 **6/6 拉取运行成功**。该地址当前由 DHCP 分配，尚无 IPAM/DHCP 保留证明；不能硬编码为产品默认值。
- Sentinel middleware（PostgreSQL、MySQL、Nacos、Consul、Prometheus、node-exporter）、T003 Console（2/2）、Collector、PostgreSQL 与两个 Envoy Gateway 数据面均已恢复 Ready，两个 Gateway 当前 `Programmed=True`；Console `/healthz` 返回 HTTP 200，Prometheus 中 node-exporter 的 `up` 查询返回 6 个节点样本且均为 1。setup-window 当前 closed，Console bootstrap 返回 `initialized=false`、`setupAvailable=false`，因此安装初始化与用户旅程尚未完成。

### 缺口归属与部署时机

| 配置/问题 | 归属 | 推荐处理时机与自动化边界 |
|---|---|---|
| HAProxy stats 管理源 | LB 安全配置 | 已加入部署参数 `LB_STATS_ALLOWED_CIDR`：默认仅 `127.0.0.1:7000`；显式提供非默认 IPv4 CIDR 才允许该管理网段访问。远程默认走 SSH 隧道。不替用户猜管理网段。 |
| 本地 HTTP 镜像仓库地址与 containerd 信任 | K8s 节点运行时配置，不是 LB 配置 | 已对本次 `.157` 完成 6/6 节点可达、精确 `certs.d/<host:port>/hosts.toml` 和真实镜像 pull；本机 LAN IP 由 DHCP 分配，正式部署前仍需 DHCP/IPAM 保留或稳定 DNS。Phase 2 应收集稳定 endpoint、逐节点校验 `/v2/`、按精确 `<host>:<port>` 配置 containerd 并执行真实 pull；不要硬编码本次 `.157/.59`。 |
| 应用 VIP 的 DHCP/地址管理预留 | 网络管理边界 | 部署前预留；脚本只能做 API VIP/节点冲突检查和集群内 ARP 所有权检查，不能证明 DHCP/IPAM 已排除该地址。 |
| 业务 DNS、TLS 证书及客户端信任 | DNS/PKI 边界 | Gateway host 和 VIP 确定后配置。脚本可提供 ExternalDNS/cert-manager 引导，不应自动发明 zone、issuer、凭据或客户端信任根。 |
| 客户端→VIP、LB→NodePort 的防火墙/上游 ACL | 网络边界 | 部署前检查并记录；主机脚本只管理本机明确归属的防火墙，不修改未知交换机/路由器 ACL。 |
| Gateway HTTPS NodePort 与 HAProxy 路由 | Gateway 安装后的动态配置 | `05-manage-lb.sh` 应读取实际 HTTPS NodePort；Phase 2 不固定 `32018`。 |
| HA failover 维护验收 | 运行时/运维窗口 | 已有 2026-10-22 LB 安装 E2E 记录覆盖 Keepalived/HAProxy failover；不得自动停止在线 LB。未来改变 VRRP 健康/优先级策略时需重新定向验收。 |
| Cilium L2、MetalLB、HAProxy/Keepalived | 不同的入口/IP 宣告方案 | 选择与目标 Service IP 相符的单一路线；不要对同一个外部 VIP/地址池重复宣告。它们是实现路线的互斥/冲突，不是每台机器都需要叠加安装的“缺配置”。 |

### 本轮变更与验证状态

- `setup-lb.sh` / `02-deploy-cluster.sh` 已新增 `LB_STATS_ALLOWED_CIDR` 部署配置；不配置时 stats 只监听 `127.0.0.1`，显式配置时 HAProxy 对来源 CIDR 做 allowlist。当前运行中的两台 LB **尚未应用该新策略**，因为本轮仅做了只读核查，且现有管理页面仍可用。
- 隔离文件系统回归 `python3 -m unittest discover -s tests -p 'test_lb_setup.py' -v`：15 项通过，覆盖默认 loopback、CIDR allowlist、拒绝 IPv6/无效 CIDR/`0.0.0.0/0`，及原有安装回滚、幂等、VIP、check-only 行为。
- `bash -n 02-deploy-cluster.sh scripts/stages/setup-lb.sh`、双语 locale JSON 解析及 `git diff --check` 均通过。
- HTTP registry 本轮已完成六节点节点网络探测、精确 host trust 与强制远端镜像拉取；修复通过临时诊断 DaemonSet 写入节点 `/etc/containerd/certs.d/10.20.1.157:55000/hosts.toml`，测试 DaemonSet 和 probe namespace 已清理。节点信任文件保留，供当前集群验收使用；宿主 `.157` 尚无 DHCP/IPAM 保留证明，因此这只是当前 lab 的临时 endpoint，不能视为稳定部署配置。
- 尚未对真实 LB 应用新的 stats 策略；DNS、TLS 根信任、上游 ACL 的状态仍按上表保持未确认。Console setup-window 当前关闭且未初始化，完整初始化与应用 E2E 尚未完成。

### Docker Engine 更新后的复验与脚本回归

- Docker Engine `29.9.2` 与 `local-registry` 均正常；当前 Mac LAN 地址 `.157` 上的 registry 返回 HTTP 200。使用 `/Users/richie696/.kube/self-k8s.yaml` 检查的 cp1–cp3/w1–w3 六节点全部 Ready；middleware、T003 Console/Collector/PostgreSQL 均 Ready，两个 Sentinel Gateway `Programmed=True`。Prometheus `up{job="sentinel-e2e-node-exporter"}` 返回六个 host 样本，均为 1。Console UI root 与 `/healthz` 均为 HTTP 200。
- 在专用测试 namespace `sentinel-t003-console-20261019` 内显式打开 setup-window；bootstrap 现返回 `initialized=false, setupAvailable=true, operatorAuthorized=false`。这只开放本次未初始化的验收 Console，不代表完整初始化或用户旅程通过；远端登录、provider 配置与页面交互仍未验收。
- 修正 `02-deploy-cluster.sh` 在 registry override 持久化分支引用未定义变量的问题，改为使用已声明的 `LOCAL_REGISTRY_HOST_OVERRIDE`；新增 `tests/test_configure_registry_mirror.py`，隔离覆盖容器信任文件生成、仅 `config_path` 变化触发 containerd restart、幂等 rerun、不覆盖外部配置、非法端口 fail-closed 和 resume 变量合同，**4 tests passed**。
- `python3 -m unittest discover -s tests -p 'test_lb_setup.py' -v` **15 tests passed**；Phase 2、LB 与 registry stage 的 `bash -n`、中英文 locale JSON parse、两个仓库 `git diff --check` 通过。5 个验收 Kubernetes YAML 用当前 `.157:55000` 渲染并通过 kubectl client dry-run；无旧 `.59/.254` 镜像字面值。未运行完整 KubeKit Phase 2，也未对在线 LB 或配置中心/用户数据库做写入。
- 当前 Console 的 HTTP port-forward `localhost:18321` 保留运行；`open_in_codex` 已请求将 `http://localhost:18321/#/setup` 放入 Codex 内置浏览器，但工具返回 queued，未把它记为已渲染/已交互的浏览器证据。Docker Desktop 本地 Kubernetes context 的 `127.0.0.1:6443` 仍拒绝连接；这不影响本次通过显式 kubeconfig 检查的六节点集群。

### 2026-10-22 Docker 更新后复测补充

- 更新后的 Docker Engine `29.9.2`、本机 registry 与 Nacos 均运行；六节点 K8s、T003 Console 2/2、Collector/PostgreSQL 和监控中间件可用。Console 当前 bootstrap 读回 `initialized=true`，与上文“初始化尚未完成”的历史观察不同；本次只读复核没有重新初始化或修改业务 DB。
- 从工作站直连 LB1 `10.20.1.12:7000`、LB2 `10.20.1.19:7000` 与应用 VIP `10.20.1.20:7000` 的 TCP 探测均成功；本轮没有抓取管理页面正文。部署脚本已实现默认 loopback stats 监听和显式管理 CIDR allowlist，但在线策略尚未应用。
- 当前 SSH `known_hosts` 中两台 LB 的 ED25519 key 与网络端呈现的 key 均不匹配。为避免把未经核验的新 key 当作可信身份，没有关闭 host-key 校验或执行远端写操作。运维通过可信主机控制台确认指纹后，才可继续应用脚本策略并检查持久配置。
- 当前缺口仍包括 registry 宿主 `.157` 的 DHCP/IPAM 保留、目标 DNS 与客户端生产 CA 信任。部署脚本应收集/验证 endpoint 和地址冲突，但不能自行替代网络/PKI 管理面操作。
- 定向回归复跑：`python3 -m unittest tests.test_lb_setup tests.test_configure_registry_mirror -v` **19 tests passed**；这证明候选安装脚本行为，不代表在线 LB 已应用新 stats policy。

### 2026-10-22 双 LB stats 在线收口

- 经用户核验当前 ED25519 主机指纹后，以 LB 管理器登记用户 `ubuntu` 和严格 host-key 校验分别登录 `10.20.1.12`、`10.20.1.19`。两台 HAProxy 配置均先备份到 `/etc/haproxy/haproxy.cfg.pre-stats-loopback-20261022`，候选配置只改 `frontend stats` 的 bind，并通过 `haproxy -c` 后原子替换、graceful reload。
- 当前两台 stats 均仅监听 `127.0.0.1:7000`，从 LB 本机及通过 SSH 本地隧道读取 stats 返回 200；工作站访问两台 LB 和应用 VIP 的 TCP/7000 均 closed。HAProxy、Keepalived 保持 active，443 listener 未变。验证用的本地 SSH 隧道已关闭。
- 这使“在线 LB 尚未应用 stats 默认策略”的历史记录失效；新策略与 `setup-lb.sh`/`02-deploy-cluster.sh` 默认 loopback 行为一致。远程查看 stats 需要 SSH tunnel；只有用户显式提供并登记允许的 IPv4 管理 CIDR 时，部署脚本才会配置远程 ACL。
- 绕过工作站 HTTPS proxy 后，`sentinel-t003.local` SNI 下 LB1、LB2、应用 VIP 以及六个 NodePort 的 `/healthz` 均返回 200。Registry `.157` 的 DHCP/IPAM 地址预留、业务 DNS/生产 CA 分发、上游 ACL 和维护窗口 failover 仍需各自确认，不由 stats 修复代替。
- 本机 `kubectl` 默认 `docker-desktop` context 的 API 仍拒绝连接；自有六节点集群通过 `/Users/richie696/.kube/self-k8s.yaml` 可正常验证，六节点 Ready。此次未运行完整集群重部署。
- **registry 地址租约复核：**本机 `en0` 当前 `10.20.1.157` 由 DHCP server `10.20.1.1` 分配，租约为 86400 秒（24 小时），未观察到 reservation/IPAM 保留事实。正确 registry 宿主端口 `55000`（映射容器 `5000`）的 `/v2/_catalog` 当前返回 HTTP 200；可用性不证明地址稳定。部署仍应要求用户提供已保留 IP 或稳定 DNS，再执行六节点 trust/pull；脚本不得默认写 `.157`。
- 安装交互提示已同步要求 registry 使用 DHCP/IPAM 保留地址或稳定 DNS，并保留“每个 K8s 节点可达”的约束；配置仍由运维显式输入。KubeKit 定向测试 **20/20 passed**，其中新增 locale 回归断言中英文都保留稳定 endpoint 要求且不出现 `.157` 默认值；部署脚本语法检查通过。该提示不代替真实 reservation/DNS 核验，也没有触发在线部署。
