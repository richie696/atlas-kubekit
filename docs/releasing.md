# Atlas KubeKit — GitHub 发布清单

本页用于维护者准备首次开源提交和后续版本发布。仓库 URL、可见性、默认分支和 tag 由维护者决定；本文不会自动创建或发布仓库。

## 项目信息

- 工程名称：**Atlas KubeKit**
- GitHub 仓库名称：`atlas-kubekit`
- 源码版本：以 [VERSION](../VERSION) 为准；初始版本待发布。
- Git 远程地址：`git@github.com:richie696/atlas-kubekit.git`
- 品牌署名：**By Atlas Richie**
- MIT 版权署名：**Atlas Richie**

GitHub description 可直接复制下面一行。该文字仅保存在文档中，创建仓库后还需填写到 GitHub 的 About/Description：

```text
面向 Ubuntu 24.04 的 Kubernetes 部署与运维工具集，提供节点初始化、高可用集群自动部署、常用插件按需安装，以及支持中英文菜单的集群健康检查。
```

## 首次公开前

- [ ] 确认 LICENSE 的版权署名与年份；原创文件使用 MIT，第三方材料保留相应来源和许可。
- [ ] 确认 README 的支持范围、版本锁定、示例 IP 和网络条件准确，未把实验室默认值当作通用云方案。
- [ ] 审查所有待提交文件及 Git 历史，清除私钥、配对码、join token、certificate-key、kubeconfig、真实配置和备份；`.gitignore` 不会清除已跟踪内容或历史。
- [ ] 确认根目录四个入口与 `scripts/stages/` 一起发布，压缩包不要遗漏内部脚本。
- [ ] 按 [贡献指南](../CONTRIBUTING.md) 做静态检查；在可销毁环境进行 01→02 新节点部署和失败重跑。
- [ ] 验证 02 新仓库路径、旧平铺包、缺文件错误及 systemd 重启续跑，特别是 machine-id 冲突引发的续跑。
- [ ] 按变化范围验证 03 插件功能和 04 的 CP/LB/worker 菜单；说明未执行项，勿复用旧报告冒充新版本验收。
- [ ] 准备可重复描述的日期、commit/tag、环境、版本、参数、断言与限制；公开证据先脱敏。

## GitHub 仓库设置

创建仓库并推送源码后，以下项目在 GitHub 网页设置，提交文档不会自动启用它们：

- [ ] 设置项目 description 和 topics，例如 `kubernetes`、`ubuntu`、`bash`、`kubeadm`、`high-availability`。
- [ ] 启用 Issues，确认缺陷/功能模板及 PR 模板可用；敏感漏洞不走公开 Issue。
- [ ] 启用 **Private vulnerability reporting**，并确认 Security 中可私密提交漏洞；见 [GitHub 官方说明](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)。
- [ ] 根据协作规模配置默认分支保护或 ruleset、PR 审核和维护者通知。
- [ ] 如加入 CI，区分静态检查与真实部署测试；不要把服务器私钥和公网可操作的部署权限交给不可信 PR。

## 初始 Git 提交

首次建立本地 Git 仓库时，可在项目根目录按以下步骤操作；已经初始化的仓库跳过 `git init`。提交前检查待提交内容，提交标题使用约定式提交格式：

```bash
git init -b main
git add .
git diff --cached --stat
git diff --cached
# 审查完毕后执行
git commit -m "feat: introduce Atlas KubeKit deployment toolkit"
```

之后用实际 GitHub 仓库 URL 配置 origin 并推送。若创建远程仓库时同时生成 README/LICENSE，需先处理远端与本地内容差异；避免覆盖已存在的历史。

## 版本发布

版本来源、同步命令与 tag 规则见 [版本管理指南](versioning.md)。发布前执行 `python3 scripts/update-version.py --check`；修改版本时只维护 VERSION，通过工具同步全部脚本并一同提交。

1. 确认 VERSION 的目标版本，将 CHANGELOG 待发布条目转为实际发布版本和日期，保留未完成项。
2. 用对应 commit 的真实验证记录说明兼容条件、升级影响和已知限制。
3. 创建 tag 和 GitHub Release，发布源码包；可将 [CHANGELOG](../CHANGELOG.md) 中对应版本条目作为 release notes，保留验证边界与已知限制，链接完整部署指南。发布时将“待发布”改为实际发布日期。
4. 仓库版和服务器运行副本是两份文件。更新源码不等于已更新 `/usr/local/lib/k8s-deploy/` 或 systemd 使用的副本；升级前确认续跑状态、备份和回退方案。

历史摘要中固定的日期、版本和源码 hash 应保留，不应改写为新版本已验证。格式或目录改动也需要明确其验证范围。
