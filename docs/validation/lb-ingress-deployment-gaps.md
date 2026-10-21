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
