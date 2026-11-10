# 锁定物料与离线部署

适用版本：0.3.0。已在网关阻断外网转发的独立集群完成首次部署与冻结源码重跑。
首批支持 Ubuntu 24.04 amd64 的原生 Kubernetes/Cilium 基座；插件、业务镜像和应用数据库
需要分别登记，不能从基座物料包推导全部应用均可离线恢复。

## 准备交付物

在可联网、已准备好的同平台参考控制面执行；它需要目标版本的基座镜像缓存、签名 APT 源、
Helm、python3-yaml 和完整工具源码。镜像集合从 kubeadm 和目标 Chart 渲染解析，构建不依赖
运行中的 Kubernetes API，因此可以在前一批集群拆除后仅保留缓存节点构建。配置文件的目标
Kubernetes/Cilium 版本须明确。

```bash
sudo bash ./06-maintain-cluster.sh --lang zh_CN \
  --materials-prepare --config /path/cluster.json \
  --output-dir /root/atlas-delivery
```

构建器用空 dpkg 状态解析完整依赖闭包，固定 Kubernetes/cri-tools 版本，校验所有包/OCI blob，
拉取指定 Cilium Chart 和经官方 SHA 校验的 Helm 二进制，并保存完整源码包。结果目录包含
`materials.lock.json`、签名本地 APT 仓库、镜像、Chart 和工具，最后才发布成功目录。
失败构建保留在私密 `failed-material-*` 目录供诊断，不会被当作可交付包。

物料 ID 绑定规范清单及各制品 SHA。通过可信交付渠道另行核对 ID；同一个包自带的哈希
只能帮助发现损坏，不能自行证明发布者身份。临时 APT 签名私钥不进入包，签名进程在构建后清理。
准备目录和日志保持私密；分享时仍须审核清单中的内部地址、用户名与来源元数据。

## 在离线节点启用

将完整工具源码和物料目录传到各节点，先验证，再以明确 ID 导入。使用完整目录，不只复制某个入口。

```bash
bash ./06-maintain-cluster.sh --lang zh_CN \
  --materials-verify --materials /path/materials-ID --material-id ID

sudo bash ./06-maintain-cluster.sh --lang zh_CN \
  --materials-import --materials /path/materials-ID \
  --material-id ID --confirm ID --output-dir /root/atlas-import-report
```

导入先核对全部文件、OCI/Chart/工具格式及 APT 签名，再安装到工具拥有的目录并激活。
已激活另一 ID 时拒绝直接覆盖。操作只启用依赖源，不代表 01/02 已执行或容器运行时已初始化。
随后按正常流程执行 01 和 02；它们会使用签名本地 APT 源，在 containerd 就绪后导入固定镜像，
从包内使用 Helm/Cilium Chart，并在 02 核对所有登记节点的物料 ID 相同。缺件或版本不匹配即停止，
不会回退公共源。离线模式不要求公共源 DNS，但保留节点地址、SSH、身份与集群网络检查。

离线仍需要基础内网路由。当前基座保留 kube-proxy；主机访问 Service IP 前需要路由可达，
否则 Cilium 初始化及主机侧 Service 客户端可能等待。02 会在安装前检查各 Kubernetes 节点
到 Service 网段首地址的路由；应在 01 的静态网络选项或原有网络配置中提供内网网关/明确路由。
网关可以禁止互联网转发，检查不要求连接公共网络，也不会猜测或改写管理员的网关。

离线模式安装锁定的部署依赖；完整 OS 补丁需要独立维护的离线系统仓库或经过审核的基础镜像。
单节点及完整离线集群的实际版本、失败修复和边界见 [实际执行记录](validation/operations-e2e-20261110-execution.md)。
源码包保留本项目 MIT 许可证及 `licenses/` 下的 Helm/Cilium 上游文本；APT/OCI 保留原载荷中的许可信息。

物料激活针对一次锁定交付，不提供跨 ID 在线切换。对已有集群做补丁升级前，需要单独准备
目标版本的软件包来源和镜像缓存；升级预览会检查目标包是否可用。不能把新装离线物料直接
覆盖到已激活的旧 ID，也不能把手工准备的升级仓库描述成已经实现的自动物料迁移。
节点仍有激活的离线物料时，当前补丁预览会在包命令和节点驱逐前拒绝；跨 ID 升级环境
须通过独立受控流程准备，不通过直接删除生产状态文件来绕过检查。

## 企业 HTTPS 仓库

首次用 02 部署基座后，通过 06 对已有 Kubernetes 节点配置独立的仓库信任。
公开 CA 文件必须只含 PEM 证书，禁止使用带私钥的合并文件。省略 CA 文件时使用系统信任。

```bash
bash ./06-maintain-cluster.sh --lang zh_CN --change-plan \
  --operation registry-configure --registry registry.internal:5443 \
  --ca-file /path/public-ca.pem \
  --control-target atlasops@cp1.example --ssh-identity /path/admin-key

bash ./06-maintain-cluster.sh --lang zh_CN --change-apply \
  --plan-file /path/plan.json --confirm PLAN_ID \
  --control-target atlasops@cp1.example --ssh-identity /path/admin-key
```

计划绑定 CA SHA、节点身份和当前配置；逐节点验证 HTTPS /v2/ 后应用，只处理工具拥有的 hosts.toml，
保留原配置供回退，使用版本化 CA 文件和原子替换。新 worker 加入时从已验证控制面取得公开 CA，
核对指纹后继承配置。不会改系统 CA，也不关闭 TLS 校验。

HTTPS 200/401 仅证明传输和端点验证；认证仓库仍需业务自己的 imagePullSecret 或凭据管理方案。
工具不把密码、私钥或认证令牌写入集群配置、计划及常规报告，也不自动向企业仓库推送制品。
