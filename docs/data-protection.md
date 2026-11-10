# 卷与应用数据的保护登记

etcd/配置备份不会备份 PVC 内容和应用数据库。06 的保护清单把这些对象单独列出，
不把控制面恢复成功解释为应用已恢复。

首先由应用或存储的备份工具生成一致性备份，并用 age 加密。为制品创建当前账号拥有的
0600 JSON 声明文件；密文也须为 0600 普通文件，路径中不允许符号链接。例如：

```json
{
  "schemaVersion": 1,
  "clusterIdentity": "实际-kube-system-namespace-uid",
  "provider": "age-archive",
  "createdAt": "2026-11-10T00:00:00+00:00",
  "objects": [
    {"kind": "persistentvolumeclaim", "namespace": "app", "name": "database", "uid": "实际-PVC-uid"},
    {"kind": "statefulset", "namespace": "app", "name": "database", "uid": "实际-StatefulSet-uid"}
  ],
  "artifact": {
    "path": "/备份所在管理机的绝对路径/database.tar.age",
    "size": 123456,
    "sha256": "替换为密文文件的64位小写SHA256"
  }
}
```

示例中的身份、大小、路径和校验值必须替换为实际值。对象支持 PVC、Deployment、StatefulSet，
需要填写实际对象 UID，不能只用同名对象代替原来的保护对象。创建时间需带时区且不晚于当前时间。
声明不能包含数据库密码、连接串、私钥、kubeconfig 或导出命令。私钥应独立保存。

```bash
./06-maintain-cluster.sh --change-plan --operation protection-register \
  --evidence-file /绝对路径/database-protection.json \
  --control-target ubuntu@控制面地址 --ssh-config /绝对路径/ssh_config

./06-maintain-cluster.sh --change-apply --plan-file /生成的计划.json \
  --confirm 完整计划ID \
  --control-target ubuntu@控制面地址 --ssh-config /绝对路径/ssh_config

./06-maintain-cluster.sh --protection-status \
  --control-target ubuntu@控制面地址 --ssh-config /绝对路径/ssh_config
```

计划和执行都在运行 06 的管理机核对密文。记录写入该集群自有 ConfigMap，包含公开的声明、
制品大小/SHA 和校验状态，省略本机路径、私钥及制品正文。重复登记同一内容幂等，
不同对象或不同制品生成新记录，不能静默覆盖旧记录。确认后不会自动上传、删除或恢复制品。

保护清单默认检查全部 PVC；需要纳入应用清单的 Deployment/StatefulSet 可设置
`atlas-kubekit.io/data-protection: required` 注解。已登记的工作负载也会纳入核对。
脚本不会根据镜像名称猜测所有数据库；未标记且未声明的外部数据库仍需独立登记和管理。

报告区分以下事实：

| 状态 | 含义 |
| --- | --- |
| 未登记 | 当前对象没有匹配 UID 的记录，备份与恢复均未验证 |
| 已登记、恢复未验证 | 登记时的密文大小/SHA 已核对，声明中的对象当时存在；不能据此证明备份内容、一致性或可恢复 |
| 身份变化 | 原对象已缺失或重建，旧记录不能覆盖新的 UID |
| 被当前身份登记取代 | 当前 UID 已有新记录，旧 UID 记录保留为历史，不再算作当前身份缺口 |

登记核对的是 age 格式头和密文字节完整性；未持有解密身份，不能验证 age 认证或明文内容。
后续状态检查也不代表远端备份文件当前仍可下载。应使用对应存储/数据库工具进行隔离恢复，
检查数据、应用读写和依赖服务，保留恢复证据。此版本不执行任意 SQL dump、在线数据库恢复或生产原位覆盖。
保护状态仍有未验证项时退出码为 2；发现过时身份时为 1，不以 0 表示应用灾备通过。
