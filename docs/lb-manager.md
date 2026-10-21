# LB 运维管理器（试用版）

`05-manage-lb.sh` 是部署流程完成后的交互式 LB 运维入口，用于现有 Atlas KubeKit `lb1/lb2` 节点上的 HAProxy 与 Keepalived。它持续读取服务状态，并可管理脚本自己标记的业务配置。

## 运行

在能 SSH 到 LB 节点的任一集群管理机上运行。不要用 `sudo` 启动本地脚本，以便复用当前账号的 SSH 私钥并将清单保存到该账号目录：

```bash
bash ./05-manage-lb.sh
```

首次运行后进入“LB 节点清单”，设置远端 SSH 用户（默认 `ubuntu`），再一次粘贴节点，例如：

```text
lb1=10.20.1.12 lb2=10.20.1.19
```

节点清单、SSH 用户和外部 LB 登记项保存在 `~/.config/atlas-kubekit/lb-manager.json`（权限 0600）。本机需要 Bash、Python 3、OpenSSH；目标 LB 需要已由 02 号脚本配置 HAProxy/Keepalived、可用的 `sudo -n` 和配置校验命令。若目标节点不允许 SSH 免密或远端 sudo 需要密码，脚本会停止，不会提示输入或保存密码。

## 菜单

1. **实时状态**：每 3 秒刷新节点连通性、HAProxy/Keepalived 服务状态、IPv4 地址、两份配置校验结果、VIP 所有者，以及 stats socket 可用时的前后端状态和会话计数。按 Ctrl+C 返回菜单。
2. **业务 VIP**：增删列入受管清单的 Keepalived VIP。Kubernetes API VIP `10.20.1.9` 不允许通过此菜单改动。
3. **HAProxy 转发规则**：添加或移除按前端端口转发的 HTTP/TCP 规则，后端填写为逗号分隔的 `IPv4:PORT`。转发到 Kubernetes 应用时，填写 LB 可达的 NodePort 或 LoadBalancer VIP；默认不要把集群内 ClusterIP 当作 LB 后端。可选择受管业务 VIP 或 `*` 监听；02 配置的 `ip_nonlocal_bind=1` 允许备用节点预先绑定业务 VIP。HTTP 规则可设置较长的 WebSocket tunnel timeout；这不保证故障切换后保留既有连接。
4. **自有 LB 节点清单**：批量替换、增加、移除节点。它只维护目标清单，不会创建虚拟机、初始化 Ubuntu、安装 HAProxy/Keepalived 或配置新节点。新增节点必须先按集群部署方案准备好，并使用相同的 Keepalived 实例和网络参数。
5. **外部 LB 清单**：登记/移除设备或云 LB 的地址，便于记录。此版本不会连接外部厂商 API，也不会向外部设备下发配置。

## 配置安全和边界

- 每次变更先通过 SSH 检查所有自有 LB 服务与配置；再在所有目标节点生成临时候选文件，并运行 `haproxy -c` 与 `keepalived -t`。全部通过后才应用。
- 只更新 HAProxy 的 `ATLAS KUBEKIT MANAGED ROUTES` 区块和 Keepalived `VI_KUBE_APISERVER` 实例中标记的业务 VIP。已有 API VIP、其他前端/后端与未标记 Keepalived 配置会保留。
- 应用前在每台 LB 创建 root-only 备份：`/var/backups/atlas-kubekit/lb-manager/<timestamp>/`。脚本逐节点应用；如任一节点失败，会尝试从本次暂存副本回滚已处理节点。
- HAProxy 使用 reload；Keepalived 使用 systemd reload。若目标 Ubuntu 的 Keepalived 单元不支持 reload，变更会失败并回滚。脚本会显示 reload/漂移影响提示。
- 当前版本按监听端口分流，不提供同端口域名/路径规则、TLS 终止、证书、访问控制、健康检查策略配置、灰度发布或多集群故障切换。HAProxy 后端当前用 TCP `check`。
- VIP 必须是已由网络管理员预留、与节点 IP 和 API VIP 不冲突的地址。脚本不判断 DHCP/网络设备上的空闲状态，也不探测外网可达性。
- 本版本是为现有集群试运行的初稿。先用非关键业务 VIP/端口验证：服务状态、配置校验、VIP 漂移、HTTP/TCP 转发、WebSocket 长连接和回滚路径，再作为生产变更入口使用。

## 现有环境试用建议

1. 先登记 `lb1=10.20.1.12 lb2=10.20.1.19`，打开实时状态，确认两台服务均 active 且配置检查通过。
2. 先登记一个预留的业务 VIP（你之前指定的 `10.20.1.20` 可作为测试候选），再用一条非生产测试端口规则转发到可访问的测试 Pod/服务。
3. 从同网段客户端访问业务 VIP 的对应端口；另行观察 VIP 所有节点及 HAProxy 状态。
4. 测完后用菜单移除测试路由和业务 VIP。API VIP `10.20.1.9:6443` 不应受影响。

故障时先保留屏幕错误信息，并检查两台 LB 的备份目录、`systemctl status haproxy keepalived` 与 `journalctl -u haproxy -u keepalived`。不要直接手工替换完整配置文件。

如果代码还未在 cp1 的 `~/deploy`，在本机仓库根目录复制完整项目并保留相对目录，然后登录 cp1 运行；请保持终端交互，不要通过非交互 SSH 执行菜单：

```bash
ssh ubuntu@10.20.1.13 'mkdir -p ~/deploy'
scp -r ./. ubuntu@10.20.1.13:~/deploy/
ssh ubuntu@10.20.1.13
cd ~/deploy
bash ./05-manage-lb.sh
```

如果你的 cp1 地址或 SSH 用户不同，替换示例值。脚本应以普通用户启动；它会使用本机 SSH 凭据连接 LB，并在 LB 上调用 `sudo -n`。
