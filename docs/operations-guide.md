# 06 持续运维操作指南

适用版本：0.3.0。读者：集群管理员。
提供安全/服务报告、加密备份与隔离恢复、配置与变更计划、worker 生命周期、补丁升级、
诊断包、物料管理和外部数据保护登记。各能力有独立保护范围及验证记录，不执行生产原位恢复。
使用完整项目目录，目标主机为 Ubuntu 24.04 amd64、Python 3；管理机可执行本地报告和 SSH 采集。

## 安全检查和报告

在项目根目录执行，未指定动作且有交互终端时显示菜单：

```bash
bash ./06-maintain-cluster.sh --lang zh_CN
bash ./06-maintain-cluster.sh --lang zh_CN --report --output-dir "$HOME/atlas-ops-reports"
bash ./06-maintain-cluster.sh --lang zh_TW --security-check
```

远端采集使用当前账号的 SSH 身份，要求已有可信 known_hosts；不会自动接受新主机指纹：

```bash
bash ./06-maintain-cluster.sh --lang zh_CN --report \
  --target ubuntu@10.20.1.13 --sudo \
  --ssh-identity "$HOME/.ssh/id_ed25519" \
  --output-dir "$HOME/atlas-ops-reports"
```

`--sudo` 仅控制远端 `sudo -n` 只读采集，不能在本机自动提权。无权限时标记 UNVERIFIED，
不输出“安全通过”。只读检查不会删除 bootstrap sudo；实际安全收尾需要独立管理身份和显式计划。
stats 检查仅覆盖 TCP/7000 监听，非回环监听需另核对来源 ACL，不直接判定为未授权暴露。

输出目录必须归执行账号所有且权限为 0700；新目录自动按该权限创建，已有宽松目录会被拒绝。
每次在 `<output>/<runId>/` 写入 0600 的 `report.json` 与 `report.txt`，不会覆盖旧报告。

02 部署退出、已完成部署的只读复核和 `--check` 审计也保存统一原生摘要，04 每次选中的
检查动作保存摘要；默认 root 路径为 `/var/lib/k8s-deploy/reports`，普通审计账号使用
`~/.local/state/atlas-kubekit/operations`。摘要记录该动作的结果/错误/跳过，不复制原始特权
输出，也不代表没有选中的功能全部验收通过。部署实际完成但报告失败时，会明确提示先核对
实际状态再决定是否重跑；帮助命令不作为一次审计通过记录。

退出码：0 为本次检查无异常；1 为失败；2 为需核对的 WARN/UNVERIFIED 或参数用法错误
（查看报告/错误信息区分）；130 为用户取消。备份有明确覆盖限制，通常返回 2，不能仅凭退出码
将其解释为备份失败。JSON 中 `result` 和单项 `status` 说明实际结果。

## 统一配置与差异检查

统一配置操作也由 06 提供：`--config-template`、`--config-check`、`--config-import`、
`--config-export`、`--config-drift`。格式、幂等导入、revision 冲突保护、只读差异与 02 接线详见
[统一集群配置](cluster-configuration.md)。这些操作不会创建备份或应用集群变更。

## 备份前准备

备份/校验必须在源节点以 root 执行，`--target` 只用于只读采集。
备份需要 [age](https://github.com/FiloSottile/age)；控制平面还需要与当前 etcd 匹配的
`etcdctl`、`etcdutl`。程序检查依赖，不自动更新集群组件。

在安全管理机生成并保管 age 密钥，只把公钥接收者提供给备份节点：

```bash
umask 077
age-keygen -o atlas-backup-identity.txt
age-keygen -y atlas-backup-identity.txt
```

创建备份只需要 `age1...` 公钥；私钥应放在集群外的安全位置。使用示例前替换占位符，
私钥和未脱敏备份不得进入 Git、工单或聊天。

## 创建和校验

```bash
sudo bash ./06-maintain-cluster.sh --lang zh_CN --backup-plan \
  --scope control-plane --output-dir /var/backups/atlas-kubekit/operations

sudo bash ./06-maintain-cluster.sh --lang zh_CN --backup-create \
  --scope control-plane --recipient 'age1替换为实际公钥' --confirm BACKUP \
  --output-dir /var/backups/atlas-kubekit/operations
```

`configuration` 备份存在的白名单配置：hosts/fstab、kubeadm 配置与 PKI、containerd、
SSH 服务配置（不含 SSH 私钥）、HAProxy、Keepalived、LB 管理状态、LB sysctl 和部署计划。
`control-plane` 在此基础上增加本机 kubeadm etcd 快照；当前仅适配
`https://127.0.0.1:2379` 和标准 `/etc/kubernetes/pki/etcd/` 路径，不适配外部 etcd。

**两个范围都不覆盖持久卷和应用数据库。** 配置复制与 etcd 快照不是跨系统事务快照，
恢复前需检查配置版本与备份时间是否对应；应用级一致性需要相应的备份能力。

生成 `<output>/backup-<runId>/`，包含 0600 的 `payload.tar.age` 和不含正文的 `backup.json`。
原始配置和 PKI 只在 root-only 暂存目录处理，完成/正常失败后清理；SIGKILL 或断电可能留下
私密暂存目录，应检查 `.partial-*` / `.verify-*`，不要将其作为成功备份上传。

在持有 root-only age 私钥文件的节点校验：

```bash
sudo bash ./06-maintain-cluster.sh --lang zh_CN --backup-verify \
  --backup /var/backups/atlas-kubekit/operations/backup-替换执行编号 \
  --identity /root/secure/atlas-backup-identity.txt \
  --output-dir /var/backups/atlas-kubekit/verification
```

校验检查密文、age 解密完整性、清单一致性、文件哈希、重复/越界路径和保护范围。
SHA-256 和公钥加密不等于证明制品由哪位管理员签发；外部存储仍需访问控制和可信来源管理。
第一批不自动上传、配置保留周期或安装定时任务。

## 恢复计划

```bash
sudo bash ./06-maintain-cluster.sh --lang zh_CN --restore-plan \
  --backup /var/backups/atlas-kubekit/operations/backup-替换执行编号 \
  --identity /root/secure/atlas-backup-identity.txt \
  --restore-target /var/tmp/atlas-isolated-rehearsal \
  --output-dir /var/backups/atlas-kubekit/restore-plans
```

目标必须尚不存在，且不能位于 `/etc`、`/var/lib` 等生产路径。上述命令只生成计划，
不创建目标目录、不停止服务、不恢复 etcd；JSON 明确记录 `planExecution: not-executed`。
真实恢复需先确认目标身份、etcd 版本、配置对应关系、隔离环境及后续验证步骤。
恢复设计依据 [Kubernetes etcd 运维说明](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)，
不能将备份校验通过当作恢复演练已完成。

## 执行隔离恢复

在持有备份和解密身份的源控制面本机以 root 执行。先完成上面的恢复计划检查，
再指定备份中的源身份指纹和独立目标。`--restore-execute` 校验、解密、提取并恢复到新目录；
`--restore-rehearse` 还会用备份匹配的 etcd 二进制，在隔离回环端口和临时 mTLS 身份下启动、
读取原集群身份并停止自有 etcd 进程。

```bash
sudo bash ./06-maintain-cluster.sh --restore-rehearse \
  --backup /备份目录/backup-执行编号 --identity /root/secure/backup-identity.txt \
  --source-fingerprint 备份中的完整源节点指纹 \
  --restore-target /var/tmp/尚不存在的隔离目录 \
  --confirm 'RESTORE ISOLATED' --output-dir /var/backups/atlas-rehearsal-reports
```

已有目标、错误源身份、损坏制品或不匹配版本会被拒绝；不替换 `/var/lib/etcd`，不修改生产静态 Pod。
隔离 etcd 读到原身份不代表 Kubernetes 控制面已经恢复服务，也不代表卷或应用数据恢复。
卷/应用的独立登记与验证边界见 [数据保护](data-protection.md)。

## 变更的统一入口

远端集群操作用 `--control-target` 选择可读取管理员 kubeconfig 的控制面，
`--ssh-config` / `--ssh-identity` 指向管理机自己的 SSH 配置和身份。它们与只读节点报告的 `--target` 不同。
管理机不需复制管理员 kubeconfig 或部署 mesh 私钥。

```bash
./06-maintain-cluster.sh --change-plan --operation node-maintenance \
  --node w1 --maintenance-action drain \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config

./06-maintain-cluster.sh --change-apply --plan-file /生成的计划.json \
  --confirm 完整计划ID \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config
```

计划绑定集群身份、版本化参数、观察指纹和有效期。执行重新采集；目标变化、过期或旧失败计划
不能直接执行。集群锁放在固定 `kube-system`，通过 resourceVersion CAS 保证跨管理机互斥，
不因为超时就自动抢占。所有主机变更与 02/05 使用同一主机锁；05 的待提交事务另有持久预留。

| operation | 参数与行为 |
| --- | --- |
| `node-maintenance` | `--node`、`--maintenance-action cordon/drain/uncordon`；驱逐遵守 PDB 和存储限制 |
| `node-join` | 已运行 01 的新 worker：`--node wN --node-address IP --node-interface IFACE --data-device none`；创建并撤销短期 join 凭据 |
| `node-retire` | `--node wN`；驱逐、验证存储已迁出、停止 kubelet/已确认守护沙箱，按 UID 删除 Node；保留磁盘 |
| `node-replace` | `--node 旧worker --replacement-name 新worker --node-address 新IP --node-interface IFACE --data-device none`；需要不同名称/IP/机器身份 |
| `cluster-upgrade` | `--to-version vX.Y.Z`；仅同 minor 的较新补丁，控制面后 worker 串行；保留并恢复 CoreDNS 调度布局 |
| `component-upgrade` | `--to-version 1.20.Z --chart /可信的cilium-Chart.tgz`；仅受支持 Kubernetes 上的 Cilium 1.20 补丁，按节点串行 |
| `registry-configure` | `--registry registry.example:5443 --ca-file /受信任公开CA.pem`；CA 可省略以用系统信任；真实 TLS 验证后配置专属 containerd 命名空间 |
| `lb-stats` | `--stats-cidr 管理网段`；更新现有 LB stats 监听来源限制，保留业务路由 |
| `security-finalize` | `--retained-user 独立运维账号`；所有节点已具备该账号的独立 SSH/sudo，验证新通道后才移除工具自己的 bootstrap 授权 |
| `protection-register` | `--evidence-file /私密声明.json`；核对加密制品与保护对象 UID，登记公开证据 |

加入/退出/替换首批只支持 worker；控制面 etcd 成员变更需独立设计。默认不允许丢弃 emptyDir，保留节点本地数据，
存在未迁出本地 PV、Longhorn 副本或不能证明已解除的 CSI 挂载时拒绝退出。
确需丢弃 emptyDir 时，在预览阶段显式增加 `--delete-emptydir`，不能用它绕过本地 PV/CSI 保护。

Cilium 升级先核对所有目标主机身份并缓存 digest 镜像，再以 OnDelete 冻结 agent/envoy 自动替换。
operator 零不可用、最多一个额外副本；每个节点经 PDB eviction、镜像/Ready/Cilium 状态检查后继续。
最后以同一 Chart 恢复 Helm/API 两处的原更新策略。恢复策略的渲染如还会改变 Pod、Secret 或 ConfigMap，
流程会停止；不会自动强制回退 CRD 或删除 pending Helm 记录。

## 中断后继续

从 0.2.0 更新工具时，先保留完整新源码目录，不重新执行 01/02 来升级已有集群。
已有集群尚无共享配置时，提供 `--config /绝对路径/existing-cluster.json`；其中节点、网络、
SSH 用户及 Kubernetes/Cilium 版本填写**当前实际值**，不能直接保留模板的新装默认版本。
先运行配置校验/漂移检查和备份，再生成专属升级计划。节点包管理辅助代码随 06 固定代码包
分发并在私密临时目录使用，不要求旧节点先安装新版的 `/usr/local/lib/k8s-deploy` 布局。

```bash
./06-maintain-cluster.sh --change-status --plan-id 旧计划完整ID \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config
./06-maintain-cluster.sh --lock-status \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config
```

先确认原协调器和远端作业确已停止，排查报告/主机私密日志。网络超时不代表远端作业结束。
只有确认这一事实后，才使用查询得到的完整 owner 和 resourceVersion：

```bash
./06-maintain-cluster.sh --lock-recover \
  --lock-owner 查询所得owner --resource-version 查询所得版本 \
  --confirm 'OLD COORDINATOR AND REMOTE JOBS STOPPED' \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config

./06-maintain-cluster.sh --change-continue --plan-file /旧失败或取消计划.json \
  --control-target ops@控制面地址 --ssh-config /绝对路径/ssh_config
```

继续会生成新 ID 和有效期，重新采集事实，再按新的完整 ID 确认执行。已完成的节点不重装，
原计划拥有的维护标记或 Cilium rollout owner 可继承；管理员原先暂停的节点不会被擅自恢复调度。
失效 owner/RV 无法解锁，不能删除锁 ConfigMap 或直接修改日志来规避这些检查。

安全收尾只处理工具自己的 sudo 文件、配对服务及令牌，不修改用户自建规则和整个 authorized_keys。
保留账号需提前用独立密钥验证；删除 bootstrap 后，02 的旧账号/mesh 通道可能不再有权限，
05 的 SSH 用户与 06 的控制面连接也应切换到保留身份。不要为“修好连接”复制旧部署私钥到新账号。

## 诊断与物料

`--diagnostics --control-target ...` 生成有大小/条目限制的 API 白名单诊断包与 SHA 清单，
不打包 Secret、Pod 环境、原始事件消息、私钥、kubeconfig 或任意主机日志。
制品仍含集群名称、地址等环境元数据，分享前应审核。

物料准备、可信 ID 校验、离线导入和目标平台限制见 [离线交付](offline-delivery.md)。
默认组件版本只在 `scripts/lib/component-versions.json` 定义，Shell、Python、节点安装副本、
SSH 运行包和物料源码包使用同一份清单。现有集群的版本从其共享配置读取；编辑清单不会升级既有集群。
新装固定版本已变更时，旧物料和旧 02 计划应重新核对，不能混用版本后强行续跑。
