# 可销毁环境验收驱动

此目录是测试源码，CI 仅执行可移植回归，不持有服务器凭据，也不自动运行真实部署。
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

按 [离线交付](../../docs/offline-delivery.md) 将**同一物料包**分发至全部节点并激活。
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
应用保护证据与 etcd 恢复，应各自按 [验收矩阵](../../docs/validation/operations-acceptance.md)
留存独立结果和清理证明；不能从该驱动退出 0 推断全版本验收通过。
