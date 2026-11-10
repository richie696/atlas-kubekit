# 0.3.0-dev.1 持续运维第一批验证

日期：2026-11-09（Asia/Shanghai）。范围：[持续运维功能](../operations.md) 的
安全/角色服务报告、配置/etcd 加密备份、制品校验与恢复计划。
本记录不代表 0.3.0 所有规划、实际恢复或安全收尾变更已完成。

## 授权与环境

用户提供 Proxmox 测试环境，并明确允许测试时通过命令行启动虚拟机。
先读取清单，再启动 lb1/lb2、cp1～cp3、w1～w3 共 8 台；模板未操作。
启动后 8 台可 SSH，6 个 Kubernetes 节点均 Ready。
真实备份在 cp1 执行，其他节点仅执行只读采集；未重装集群或调用 02 部署流程。

基线：Ubuntu 24.04.5、Kubernetes v1.36.5、containerd 2.2.1、etcd 镜像 3.6.8-0。
测试用 etcdctl/etcdutl 3.6.8 从该节点已有镜像提取，仅放入私密测试目录。
cp1 补装 age 1.1.1；APT 初次遇到 unattended-upgrades 锁，等待锁释放后正常安装，
未强制终止包管理器。初次镜像挂载方式受环境限制失败，改用镜像导出读取指定工具，
没有修改 containerd 配置或重启运行时。

## 自动化与静态检查

```text
python3 -m unittest discover -s tests -v
46 tests passed, 49.962 seconds

python3 scripts/update-version.py --check
0.3.0-dev.1 metadata matches in 14 scripts

python3 -m scripts.lib.i18n --validate
Locale catalog structure is valid

python3 scripts/sync-storage.py --check
python3 scripts/sync-addon-sources.py --check
bash -n 06-maintain-cluster.sh
git diff --check
```

全套含 24 项新增运维回归，以及原工作区已有的 22 项回归。
新增回归使用隔离目录及受控替身；真实 age/etcd/SSH 结果见下表，不以替身证明真实链路。

## 实际链路结果

| 场景 | 观察结果 |
| --- | --- |
| 8 节点 SSH 报告 | 8/8 生成 JSON/文本报告，角色对应服务均 PASS；保留权限等项目返回 attention/退出 2，未被误判为无风险 |
| 安全检查 | 发现保留的 bootstrap NOPASSWD:ALL；配对服务未运行、SSH 策略符合基线；未删除授权或停止服务 |
| 控制平面备份 | cp1 真实配置/PKI 与 etcd 快照成功加密发布；最终计划列出 47 项配置源资源 |
| 快照工具 | etcdctl 3.6.8 创建快照，etcdutl 3.6.8 校验；加密前检查数据规模与暂存空间 |
| 加密校验 | 真实 age 解密成功，逐文件大小/哈希与清单一致；未解包到生产路径 |
| 恢复计划 | 输出隔离目标、源指纹、资源和动作；planExecution 为 not-executed，目标目录未创建 |
| 篡改密文 | 拒绝，在结构化报告中记录 FAIL / ops.error.integrity |
| 不安全私钥权限 | 0644 私钥被拒绝，记录 ops.error.identity |
| 生产恢复路径 | /var/lib/etcd 下的目标被拒绝，没有恢复行为 |
| 并发锁 | cp1 实际锁内再次申请同一主机写锁被拒绝，释放后可再次获得 |
| 无扰动检查 | 受保护 manifest/sudo 文件哈希不变；最终复查 containerd/kubelet PID 不变，API readyz 返回 ok，6 节点 Ready |
| 私密材料清理 | 无 .partial-* / .verify-*；测试 age 私钥、篡改副本、备份密文、工具及镜像导出已移除 |

最终真实链路使用的 `scripts/operations/backup.py` SHA-256：
`7a8577bae0cc98a98cde6c71d4e12fb7e92bb8bba12cdb7bb29db2a97dc51c93`。
版本号相同不代表源码相同；其他工作区未发布修改与本次新增代码均尚未提交。

## 留存与边界

- 8 台虚拟机保持运行；age 包保留。临时复制的 etcd 工具已移除，不会替换系统组件。
- cp1 私密报告/摘要留在 `/var/lib/k8s-deploy/operations-e2e-20261109/`；
  可试用代码在 `~/deploy/operations-e2e-20261109/atlas-kubekit/`，未覆盖 systemd 使用的旧部署副本。
- 报告只按白名单保存事实/结果，不包含配置正文、私钥或快照内容；它仍可能包含内部节点信息，分享前需审核。
- 测试备份已清理，不应视为集群当前的生产灾备副本；后续正式备份应使用外部保管的密钥及存储策略。
- 未执行生产/隔离 etcd 恢复、持久卷/数据库恢复、权限收紧变更、定时/异地备份、
  新增扩缩容/滚动升级、物料离线交付或 GitHub CI。上述范围继续按产品定义推进。
