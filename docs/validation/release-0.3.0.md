# Atlas KubeKit 0.3.0 发布验收

- 发布日期：2026-11-10（Asia/Shanghai）。
- 版本/tag：`0.3.0` / `v0.3.0`；比较基线：`v0.2.0`。
- 上版提交：`0e690e2290d1e789375813a9583ff8f67573d8ad`；最终源码由 `v0.3.0` 固定。
- 维护者在本轮开发与隔离验收完成后，明确授权更新 CHANGELOG/README、提交并推送、创建 tag 和 GitHub Release。

## 验收与源码边界

本轮已定义功能完成，详细范围见 [交付记录](operations-delivery-0.3.0.md) 与
[逐批实际执行记录](operations-e2e-20261110-execution.md)。验收开发源码为 `0.3.0-dev.2`；
发布整理只同步 VERSION、14 个入口/阶段的生成版本区块与正式文档，不修改业务逻辑。
历史报告、环境、源码包 SHA、物料 ID 和测试耗时保留原值，不冒称正式版重新部署过集群。

实际隔离验证包含完整离线初装/冻结源码重跑、跨管理机 CAS/SIGKILL/恢复、七节点安全交接、
age 备份/独立 mTLS etcd 恢复、worker 生命周期、Cilium 补丁与中断继续、企业 CA/CRI、
外部保护证据及 LB 部分发布后重启恢复。Kubernetes v1.36.5→v1.36.6 五节点串行升级通过，
CoreDNS 三控制面分布与 PDB 保持，完成计划幂等和升级后跨节点 Service DNS 读写通过。
旧辅助目录缺失时的 ZIP/APT 模拟验证及活动离线物料提前拒绝另按其边界记录。

发布前统一校验：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate.py
git diff --check
git diff --cached --check
```

开发冻结时 macOS Python 3.9 和 Ubuntu Python 3.12 各 180 项回归通过；正式版本再次运行
同一校验，并在推送后核对对应提交的 GitHub Actions。CI 的实际结果以对应提交运行记录为准。
首次托管执行的两个 Ubuntu 组合通过，macOS 14 任务因 runner 配置不受支持而在执行前取消；
仅将 CI 标签修正为 `macos-15-intel` 后重新执行四组合，不将取消记录当作测试通过。

## 制品和清理

源码提交后从对应提交/tag 生成 `atlas-kubekit-0.3.0.tar.gz`、`atlas-kubekit-0.3.0.zip`
及 `SHA256SUMS`，保留完整运行目录、许可证与文档。附件不包含私钥、kubeconfig、真实节点状态、
原始特权日志、加密备份、VM 磁盘或大型离线二进制物料。
Git 分支、annotated tag、Release 的非草稿/非预发布状态及附件 SHA 分别核对。

测试 VM 211～220 及最终批 221～227、209、201 均已删除，测试 VM 配置内存占用为 0 GiB；
原用户 VM 100～108 配置哈希前后相同。发布验证不再启动测试集群。

## 使用边界

首批离线平台为 Ubuntu 24.04 amd64；不包含控制面成员增删、跨 minor 升级、跨激活物料 ID
自动迁移、通用数据库导出/恢复、认证仓库凭据管理或生产原位 etcd 恢复。
控制面备份与密文登记不能代替 PVC/应用数据库恢复，隔离验收不能代表所有生产拓扑或全程零中断。
工具源码更新不会自动升级用户已有集群，新装默认值与现有环境实际版本应分别核对。
