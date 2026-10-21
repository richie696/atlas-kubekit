# Atlas KubeKit — 版本管理

## 版本来源与当前状态

根目录 [VERSION](../VERSION) 是唯一由维护者直接编辑的项目版本来源。当前源码为 **0.2.0（待发布）**，本地可核对的上一发布 tag 为 `v0.1.0`。写入版本号不会自动创建 Git tag 或 GitHub Release，也不代表已完成新的集群验证。

版本号不包含 `v` 前缀。采用 [SemVer](https://semver.org/lang/zh-CN/) 的 `主版本.次版本.修订版本` 格式，可添加 `-alpha.1`、`-rc.1` 等预发布标识；例如 `0.2.0-rc.1`。`0.x` 属于初期开发阶段，兼容契约仍可能调整。

Atlas KubeKit 的版本与其部署的 Kubernetes/Cilium、安装的 Helm Chart 版本分开管理。

## 版本如何进入脚本

[scripts/update-version.py](../scripts/update-version.py) 使用 Python 3 标准库，枚举根目录入口和 `scripts/stages/` 中的 Bash 脚本，将 VERSION 同步到每个脚本开头的生成区块。不要手工编辑 `ATLAS_KUBEKIT_VERSION` 或生成区块。

脚本使用自己内嵌的版本：即使没有 VERSION/Git，或被安装到 `/usr/local/lib/k8s-deploy/` 由 systemd 续跑，也能显示实际副本的版本。服务器仅查询版本时不需要 Python。正常交互依赖语言运行库、目录及相应辅助程序，部署时必须保留完整项目结构；内嵌版本不等于单文件可以完成安装。

更新工具会先校验 SemVer 和所有脚本区块，再写入；保留脚本权限，重复同步相同版本不改文件。该工具不执行部署、不连接服务器、不自动提交或发布。未跟踪和未发布的新代码不会因为相同版本号而成为相同源码，应同时用 commit SHA 记录准确来源。

## 查看源码或服务器脚本版本

从项目根目录运行：

```bash
cat VERSION
bash ./01-prepare-node.sh --version
bash ./02-deploy-cluster.sh --version
bash ./03-install-addons.sh --version
bash ./04-verify-cluster.sh --version
bash ./05-manage-lb.sh --version
bash ./scripts/stages/setup-k8s-node.sh --version
```

各脚本也支持 `-V`，输出格式为 `Atlas KubeKit 0.2.0`。`--version`/`-V` 应作为独立查询使用，无需 sudo、Ubuntu 环境、交互终端或集群依赖；不会进入安装流程。项目名称和版本值不翻译。

服务器上的运行副本可独立查看，例如：

```bash
bash /usr/local/lib/k8s-deploy/01-prepare-node.sh --version
```

此命令需要该副本已更新为支持版本查询的脚本。源码仓库更新并不自动覆盖服务器副本；部署排查时同时记录仓库 commit 和实际执行文件的版本。旧脚本不支持该参数时，先确认来源再安排更新。

## 同步或变更版本

在项目根目录运行：

```bash
# 将当前 VERSION 同步到所有脚本
python3 scripts/update-version.py

# 只检查是否一致；不一致时非零退出，不修改文件
python3 scripts/update-version.py --check

# 指定新版本，同时更新 VERSION 与全部脚本（后续修复版本示例）
python3 scripts/update-version.py 0.2.1
```

也可以手工修改 VERSION 后运行同步命令。`--check` 不允许同时指定新版本。工具可从其他工作目录运行，始终以自身所在仓库的 VERSION 为来源。

## 兼容性与递增规则

| 变化类型 | 稳定版递增规则 | 示例 |
| --- | --- | --- |
| 向后兼容的问题修复 | PATCH（修订号） | `1.2.0 → 1.2.1` |
| 向后兼容的新功能 | MINOR（次版本），PATCH 归零 | `1.2.1 → 1.3.0` |
| 破坏公开契约的变化 | MAJOR（主版本），MINOR/PATCH 归零 | `1.3.0 → 2.0.0` |
| 待验收的候选包 | 目标版本加预发布标识 | `1.3.0-rc.1` |

`0.x` 阶段的公开契约仍在演进：本项目将功能扩展或需迁移的行为变化汇总为新的次版本，兼容修复递增修订号；所有升级影响必须写入 CHANGELOG，不能默认兼容。改动数量多本身不构成递增 MAJOR 的理由。进入 `1.0.0` 前应明确稳定契约和验收范围。

本次 `0.1.0 → 0.2.0` 包含第五个运维入口、审计/修复、外置国际化、插件和 LB 配置扩展，按功能次版本处理。语言依赖外置及插件编号变化需迁移，详见 [本版变更](../CHANGELOG.md#020--待发布)。待发布状态由文档标注，最终 Git tag/Release 仅在确认验收后创建；当前不将 `0.2.0` 描述为稳定版生产认证。

本项目的公开契约包括：01～05 入口名称与参数、交互配置含义、语言/运行依赖、node.conf 字段、保存的部署计划及 LB 管理状态格式，以及已有节点重跑与续跑行为。磁盘、网络、SSH 和默认拓扑变化需说明迁移与恢复方式。

## 发布步骤

1. 确认目标版本并运行同步工具，更新 [CHANGELOG](../CHANGELOG.md) 的待发布条目。
2. 完成适用的检查与真实部署/功能验证，记录 commit、参数、结果和未验证边界；旧报告不改写为新版本通过。
3. 检查版本一致性和 diff，再按约定式提交源码；确保 VERSION 与生成脚本在同一提交中。
4. 在已确认的提交上创建对应 tag，推送源码与 tag，最后创建 GitHub Release。

示例命令仅用于经过验收的版本，不会由同步工具执行：

```bash
python3 scripts/update-version.py --check
git diff --check
git status --short
# 先提交源码并检查 HEAD 对应的实际版本
release_version=$(cat VERSION)
git tag -a "v$release_version" -m "Atlas KubeKit $release_version"
git push origin main
git push origin "v$release_version"
```

tag 使用 `v` 前缀，例如 `v0.1.0`。已发布 tag 对应的内容保持不变，修复通过新版本发布；不要移动 tag 覆盖旧源码。
