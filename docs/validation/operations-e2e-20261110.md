# 0.3.0-dev.2 统一配置验证

日期：2026-11-10（Asia/Shanghai）。范围：[统一集群配置](../cluster-configuration.md) 的
配置协议、私密存储、只读差异、02 输入及身份续跑边界。
本记录不代表通用 apply、跨管理机锁、完整新装、升级或灾难恢复已经完成。

## 环境与执行范围

本机 Python 3.9.6、Homebrew Bash；cp1 Ubuntu 24.04.5 / Python 3.12.3 / Bash 5。
沿用用户授权的 8 台实验节点：lb1/lb2、cp1～cp3、w1～w3；本轮开始时已运行，无需操作 Proxmox。
实际集群 Kubernetes v1.36.5、Cilium 1.20.2，API VIP 10.20.1.9，应用 VIP 10.20.1.20。
只通过已有 SSH 身份读取节点，不重新登记密钥、不改变 sudo，也没有停止或重启集群服务。

## 自动化验证

```text
python3 -m unittest discover -s tests -v
68 tests passed

cp1: python3 -m unittest discover -s tests -p test_configuration.py -v
22 tests passed, 0.978 seconds

python3 -m scripts.lib.i18n --validate
python3 scripts/update-version.py --check
python3 scripts/sync-storage.py --check
python3 scripts/sync-addon-sources.py --check
bash -n 02-deploy-cluster.sh
bash -n 06-maintain-cluster.sh
git diff --check
```

本机全套包含原有 46 项和新增 22 项配置回归；cp1 只运行这 22 项，未声称在 Ubuntu 重跑全部测试。
覆盖无副作用规范化、非法输入/拓扑、重复键、版本适配、身份采集协议、真实文件权限与原子导出、
真实双进程 revision 竞争、存储篡改、部分采集/VIP、实际 Shell 冲突函数和身份持久化函数。
02 的身份块重写重复调用不改文件时间；生成失败时原文件不变并清理候选文件，续跑能读回 pin。
这些 Shell 用例使用隔离路径，不运行系统安装。

首次在 Ubuntu 解压 macOS tar 测试包时，3 个 CLI 用例失败：隐藏的 AppleDouble `._*.json`
被语言枚举当作目录读取。修复共同语言加载器，忽略隐藏文件，将可见无效 UTF-8 语言包明确视为
LocalizationError；增加对应回归后，保留 tar 元数据的 Ubuntu 包内 22 项全部通过。
临时传到 cp1 的源码包和测试目录已删除。

## 实际链路观察

通过 06 同一 CLI 逻辑执行校验、导入、重复导入、导出、真实 SSH 差异、错误身份、部分不可达、
revision 冲突及未来版本提示，所有操作生成独立 runId 的 JSON/文本报告。

| 场景 | 实测结果 |
| --- | --- |
| 配置校验/导入/导出 | 返回 0，规范配置往返一致；重复导入不写原文件 |
| 8 节点差异 | 81 PASS、0 FAIL、0 WARN、24 UNVERIFIED，返回 2 |
| 未验证项来源 | 旧节点缺少统一 cluster.conf，每台保存的 PROFILE、LOCAL_REGISTRY_HOST、SSH_USER 三项未验证；没有将其解释为值一致 |
| 实际覆盖 | 8 节点身份、角色、IP/网卡与服务匹配；6 个 kubelet 版本、3 个 CP 的 Cilium/Pod/Service 网段匹配；两台 LB 的 API 转发、VRRP、stats 与 VIP 配置匹配 |
| VIP 归属 | API/应用 VIP 各有一个登记 LB 持有者，归属一致；只表示本次采集时点 |
| 错误固定身份 | 仅修改本机期望配置中的 lb1 指纹，实际差异返回 1，身份 FAIL，VIP 归属标记 UNVERIFIED |
| 单 LB 不可达 | 仅将本机期望配置的 lb2 地址改为未响应地址；真实 SSH 采集失败返回 2，VIP 唯一性 UNVERIFIED；未关闭实际 lb2，也未进行故障切换 |
| 配置 revision 冲突 | 修改期望端口后不提供当前 revision 导入，返回 1 / ops.config.conflict；没有变更远端监听 |
| 未来组件版本 | 目标 Cilium 改为 1.21.0，仅校验时返回 2 / 兼容性 WARN；02 部署适配拒绝该版本，不安装它 |
| cp1 的 02 校验入口 | 非 root 在隔离源码包中执行 `02 --config-check`，返回 0，未进入 root/阶段一/安装流程 |
| 计划状态 | 差异报告为 configuration-review，planExecution=not-executed，没有应用修复 |
| 无扰动核对 | 8 节点检查前后受保护配置哈希及角色服务 PID 完全一致；API readyz=ok，6 个 Kubernetes 节点 Ready |

比较的文件包含存在的 hosts、HAProxy/Keepalived 配置、cluster.conf、bootstrap sudo 与 Kubernetes
静态 Pod manifest；PID 包含 HAProxy、Keepalived、containerd、kubelet。正常 SSH/系统日志变化不在
“文件哈希一致”的断言内。

本轮实际只读采集使用的 collector.py SHA-256：
`e11dc17d63fc335af8a8cf2b7dda39a00329b0d72a5ed6490977037b87986e84`。
Ubuntu 22 项回归覆盖的 02-deploy-cluster.sh SHA-256：
`a56fa584c1ad1da9ccf6439e40d65a72179fab39b2f99b7c03af1a9875ab4791`。

## 留存及未覆盖项

本机私密报告和摘要位于 `/private/tmp/atlas-config-e2e-20261110-aco93s43/`，包括 summary.json、
ubuntu-regression.json 与按 runId 分类的报告。目录 0700，配置/报告 0600；内容是环境元数据，
不包含登录凭据、私钥、kubeconfig 或 Keepalived 密码。它们不在 Git 仓库中，临时目录清理后可能不存在。

未执行全新 VM 上的 `02 --config` 完整安装或自动重启续跑；现有 0.2.0 安装验证不能替代这条新路径。
身份块、计划冲突和配置适配已通过隔离回归，仍需在可销毁新节点做完整部署验收。
未执行配置 apply、节点扩缩容/升级、真实 VIP 故障切换、权限删除或实际恢复。
dataDevice 对应挂载/数据安全及仓库运行时/可用性不是当前差异检查的已验证范围。
本轮没有提交、推送、创建 tag 或发布 Release，正式版仍为 v0.2.0。
