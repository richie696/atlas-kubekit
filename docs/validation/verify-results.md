# Atlas KubeKit — 04 脚本验证记录

日期：2026-10-02。环境：cp1 `10.20.1.22`、lb1 `.20`、w1 `.25`，六个 Kubernetes 节点；通过 SSH PTY 操作 04 的真实交互菜单。测试脚本复制在服务器 `/tmp`，未覆盖登录用户目录的部署脚本。

脚本 SHA256：`4db0fe5a71b86da690ed8762de07af3fb510a0be2a16702741989a68a085b1e7`。

该记录对应上述首次验证版本。随后完善了启动语言选择及中文结果、帮助、故障切换指引，并将角色菜单改为动态连续编号、语言菜单样式统一为 03 的形式；历史日志中的菜单编号属于旧版本，此前的现场验证不能当作后续界面调整已经重跑的证据。

原始日志、JSON 快照和测试配置已随本地 `e2e/` 目录清理；下文文件名仅是历史记录标识，无法再从本目录复查原始证据。部署与运行脚本不依赖这些测试文件。

## 已证明的行为

| 范围 | 操作与断言 | 证据 |
|---|---|---|
| 角色菜单 | 在 Ubuntu 上模拟 cp1/lb1/w1/未知 hostname；仅对应角色菜单可见，输入隐藏编号被拒绝 | `menu-cp1.log`、`menu-lb1.log`、`menu-w1.log`、`menu-template.log` |
| 缺少工具 / 失败恢复 | 缺少 kubectl 时隐藏 CP 集群菜单；模拟 API 失败，显示四项失败并回到菜单；随后可切换中文 | `menu-no-kubectl.log`、`api-failure.log` |
| 取消测试 | 真实 CP 上登记两个节点后拒绝创建；未执行 create/apply/delete | `network-decline.log` |
| CP 健康检查 | 节点/压力条件、全部 Pod 就绪、API readyz/livez、CoreDNS、etcd 三成员 health/status/alarm、证书到期、存储、指标查询无命令失败 | `cp-readonly.log` |
| SSH / Cilium / 网络 / RBAC | 从 cp1 到其他节点的严格主机密钥验证、hostname/IP 断言；Cilium agent status、rollout；Service/入口/策略及当前管理员权限查询 | `cp-extra-readonly.log` |
| 14 个插件的状态菜单 | 逐项读取命名空间工作负载及对应 CRD 业务资源；不存在的可选 CRD 明确跳过 | `cp-extra-readonly.log`；不等于插件完整 E2E |
| LB | HAProxy/Keepalived 服务与配置校验、统计 CSV、三个 CP 后端 TCP、VIP readyz | `lb1-readonly.log` |
| worker | kubelet/containerd active，ctr 容器、CNI/路由及到 VIP 的 TCP 查询 | `w1-readonly.log`；该机器未装 crictl，CRI health/ps/images 明确跳过 |
| 真实网络 | web 在 w1、probe 在 w2；完整 Service 域名 nslookup；通过 Service DNS、ClusterIP、Pod IP 返回预期固定内容 | `functional-network-pvc-accepted.log` |
| 真实 PVC | 已有 `addon-e2e-nfs`、100Mi、UID/GID 0/0；writer 写入断言通过，删除后 reader 读取同一内容通过 | `functional-network-pvc-accepted.log`；只覆盖 Pod 重建，不保证不同 UID 或跨节点存储 |
| HTTP 路由 | 现有 Traefik NodePort 正确 Host 返回 200、错误 Host 返回 404；故意期望 200 时明确报告失败 | `http-tls.log` |
| TLS | 临时 HTTPS 服务的标准自签证书：提供 CA + 保留 SNI 的 DNS 覆盖可返回 200；错误 hostname 或不提供 CA 被拒绝 | `http-tls.log`；临时服务随后删除 |

本地和 Ubuntu 均执行 `bash -n`；`--help` 正常。最终版本重复执行角色、缺少工具、API 失败、中英文切换、拒绝创建和 CP 基础查询，15 项回归通过，见 `regression-results.json`。完整执行历史见 `results.json`；其中保留了两项中间失败，不能将历史失败条目当作最终通过。

## 测试中发现的问题和处理

1. BusyBox `nslookup` 查询 `web.<namespace>.svc` 返回 NXDOMAIN，而 wget 的搜索路径可正常访问。04 改为读取 Pod 的 `/etc/resolv.conf`，提取实际集群域名后查询完整 Service 域名；重新执行后解析及三条 HTTP 路径均通过。初始记录：`functional-network-pvc.log`。
2. 原测试 PVC 使用容器默认 root 可写；改为 UID/GID 1000 后，在当前 NFS 卷上没有完成写入。脚本保留可配置 UID/GID，加入基于实际文件内容的 readinessProbe，并要求 writer 的写入断言成功才创建 reader，避免短暂 Ready 被误判。最终按当前卷可用的 UID/GID 0/0 通过；其他存储必须按自己的权限配置。中间记录：`functional-network-pvc-final.log`。
3. 现有 Traefik 测试证书的 Subject/Issuer 为空，本轮 Ubuntu curl 使用该证书作 CA 时返回 X509 issuer 验证错误。因此 04 的 TLS 正反向测试采用单独的临时标准自签证书服务，未修改原 Traefik 证书；不将此结果当作本轮 Traefik TLS 已通过。

## 清理与未验证边界

- 04 请求删除自己创建的随机命名空间；不删除已有业务资源，也不自动改写 StorageClass 的 Retain 策略。
- 本轮 NFS StorageClass 使用 Retain。测试结束后，测试执行者仅对 `test-namespaces.json` 中本轮命名空间的三个测试 PV 将回收策略改为 Delete，由 CSI 回收；临时 HTTPS 进程、证书和私钥目录已删除。
- 收尾查询确认：本轮测试命名空间和对应 PV 均不存在，临时 TLS 目录已删除，六个节点仍 Ready。
- 未执行：LB 故障切换、CP/etcd 故障或备份恢复、全节点双向网络、跨节点 PVC、真实云 DNS/S3/云 LB、性能压测。14 项插件的既有功能 E2E 记录仍见 [ADDONS-E2E.md](addons-e2e.md)，本次只复核其状态查询菜单。
- crictl 路径和 CRI 条件断言未在当前 worker 实跑；没有该工具时脚本明确跳过，不安装工具、不把跳过记为通过。缺少 admin.conf 的分支做了代码检查，运行模拟覆盖的是缺少 kubectl。
