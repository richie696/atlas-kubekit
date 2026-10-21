# Atlas KubeKit — 插件下载源

## 使用方式

在运行 03 的 CP 节点执行 `sudo bash ./03-install-addons.sh`，启动顺序为：**选择语言 → 选择下载源 → 检查集群 API → 插件主菜单**。使用 `--lang zh` 或 `--lang en` 时跳过语言提问，仍会先展示下载源菜单。首次默认为官方源；以后加载登录账号 `~/.k8s-deploy/addon-sources.json`（0600）中的选择，每次启动仍可修改，回车或输入 `0` 保留当前源并继续。主菜单仍可输入 **S** 更换下载方案，输入 **L** 切换语言。

本设置不是每个插件各问一次，也不会只修改 Envoy Gateway。HTTP Helm 索引和 Chart 包、OCI Chart 都通过同一个策略入口；安装镜像根据该方案处理。选择本身只保存配置，不会升级已有 release。

## 内置方案与来源

| 选项 | 方案 | 普通 Helm 文件 | Docker Hub OCI / 镜像 | 其他公开镜像仓库 |
| --- | --- | --- | --- | --- |
| 1 | 官方源（默认） | 各插件官方仓库 | `docker.io` | 原始 GHCR、Quay、registry.k8s.io 等 |
| 2 | DaoCloud（国内） | `https://files.m.daocloud.io/<原域名>/<路径>` | `m.daocloud.io/docker.io/<路径>` | `m.daocloud.io/<原仓库>/<路径>` |
| 3 | 1Panel + DaoCloud（国内） | DaoCloud 文件代理 | `docker.1panel.live/<路径>` | DaoCloud 多仓库代理 |
| 4 | Google 缓存（国际） | 官方源 | `mirror.gcr.io/<路径>`，仅部分缓存 | 官方源 |
| 5 | 自定义企业代理 | 用户提供的 HTTPS 前缀 | 用户提供的仓库前缀，保留原仓库路径 | 同左；需自行同步/配置缓存 |

这里列的是可选择的来源方案，不是可用率或流行度排名，也不表示某服务包含全部插件及版本。1Panel 仅覆盖 Docker Hub，因此第 3 项明确组合 DaoCloud；Google 只缓存部分 Docker Hub 内容，尤其不能假定包含 Envoy Helm OCI artifact。未命中、限流、TLS、DNS 或超时都会失败，不静默切换其他源。

文件下载支持所选源的 HTTPS 重定向。DaoCloud 及组合方案允许 `files.m.daocloud.io` 跳转到已核实的 `files-mirror.r2.daocloud.vip` 下载域名；它是该源的 CDN 路径，不代表切回上游。日志显示跳转前后的域名，不输出临时签名参数。自定义文件代理默认仅允许同域名、同端口跳转；未知跨域、跳回上游、含账号密码或降级 HTTP 的跳转会被拒绝并显示具体域名。未来服务若新增 CDN 域名，需核实后更新规则。

来源与协议依据（2026-10-18 查阅）：

- [DaoCloud 镜像服务](https://github.com/DaoCloud/public-image-mirror)：说明添加原仓库前缀的路径规则、支持范围、白名单和限流。
- [DaoCloud 文件服务](https://github.com/DaoCloud/public-binary-files-mirror)：说明 `<代理>/<原域名>/<路径>` 的 HTTPS 下载方式。
- [1Panel 容器设置](https://1panel.pro/docs/v2/user_manual/containers/setting/)：列出 `docker.1panel.live` Docker Hub 加速地址。
- [Google Docker Hub 缓存](https://docs.cloud.google.com/artifact-registry/docs/pull-cached-dockerhub-images)：仅缓存常用公开镜像，没有保证任一版本长期存在。
- [Helm OCI 文档](https://v3.helm.sh/docs/v3/topics/registries/)：OCI Chart 是 registry artifact，普通 Helm 仓库仍需要 HTTP 索引和包下载。

没有把阿里云、腾讯云和华为云的域名直接当作公共全量镜像源。账号专属 ACR/TCR/SWR 加速地址或缓存项目需由用户创建，且不同产品的路径、白名单、网络与鉴权约束不同。准备好下面约定的代理后可选第 5 项。

## 下载和安装顺序

1. 选择语言、检查本机工具后，加载已保存的源并立即展示下载源菜单。确认选择后检查集群 API，进入插件主菜单。
2. 用户选插件、阅读说明并确认，填写专属参数。
3. 所有插件的版本及镜像处理均检查 `python3-yaml`；缺少则询问是否用当前 Ubuntu apt 源安装，默认不安装。Ubuntu apt 源不由此菜单修改。
4. 普通 Helm 仓库统一按所选源下载 `index.yaml`，按实际 Kubernetes 版本筛选稳定候选，再显示默认值供修改。也允许明确输入索引中已有的预发布版本，但仍需兼容检查。包 URL 即使指向 GitHub Releases，也按同一文件策略处理。所有来源均检查索引 SHA256 和包的名称/版本后安装本地包。
5. Envoy Gateway 从官方发布说明目录发现稳定版本，并用官方兼容表筛选候选，不再固定默认 Chart 版本。OCI Chart 先映射 registry，再由 Helm 下载所选版本到临时目录；Helm 负责 OCI 摘要校验和认证。
6. 根据 Chart 默认 values、已有 release values 和本次额外 values 生成最小镜像覆盖，再用 Helm post-renderer 处理资源中的镜像。覆盖 Deployment/DaemonSet/StatefulSet/Job/Pod 等容器与 initContainer、Prometheus/Alertmanager 资源镜像及 operator 镜像参数。镜像版本或摘要保留。
7. Helm hook 不经过 post-renderer，因此提前转换常见 `registry` / `repository` / `tag` 镜像 values。未知 Chart 结构或在模板中硬编码的 hook 镜像可能需要额外 values 或 Chart 适配；不要把此机制理解为对任意未来 Chart 的保证。
8. Envoy 的 `envoy-gateway.yaml` ConfigMap 也处理镜像。Envoy Proxy 镜像自动按下面的规则解析并显示依据，不再需要填写镜像坐标；Chart 版本不是 Envoy 数据面版本，不据此拼接 tag。
9. 下载后重新检查实际 Chart 元数据，按集群版本和可用 API 预渲染，检查资源所需 API 版本。通过后使用 `helm upgrade --install --wait --atomic` 安装并等待。完成后返回插件菜单；失败时停止并保留源选择，重跑修复。

镜像转换是资源与 values 转换，不写节点的 `/etc/containerd/certs.d`，不重启 containerd，也不部署修改全体 Pod 的 Webhook。新安装/升级的资源显式引用所选镜像地址，所以调度到其他 CP/worker 后也使用该地址；各节点仍需能访问该服务。

## 全部插件的版本与 Kubernetes 匹配

此规则覆盖菜单全部 16 项、17 个 Helm release（Loki/Alloy 分开安装），不限于 Envoy Gateway。Headlamp 和 Longhorn 也通过同一套 Chart 稳定版本筛选、下载源映射、Kubernetes 声明范围检查及预渲染流程。

1. 启动时从当前 kubeconfig 指向的 API `/version` 读取服务端版本，不使用 kubectl 客户端版本，也不假定它等于 02 的默认版本。
2. 普通仓库按稳定 Chart 版本从新到旧筛选，跳过弃用和明确不匹配的版本。`kubeVersion` 范围由 Helm 自身的 SemVer 引擎判断，支持 `||`、通配符等语法。推荐值为有依据且符合当前版本的最高稳定候选；手工改版本后也重新检查下载包。
3. Metrics Server、cert-manager、Envoy Gateway、KEDA、Kyverno 另外动态读取官方兼容表，并通过 Chart 的 **appVersion** 匹配应用发行分支。Chart 版本号和应用版本号分别处理。表中有当前分支时，两组约束都必须满足；不根据日期或版本号推测支持范围。
4. 检查结果区分 `UPSTREAM`（符合已解析的官方范围）、`CHART`（仅符合 Chart 声明）和 `UNKNOWN`（兼容性未确认）。并非每个项目都提供统一格式的完整兼容表；只写最低版本的 Chart 也不能证明任意未来 Kubernetes 版本已获上游测试。官方表不可达、格式改变、应用分支未列出，或缺少 `kubeVersion` 时，不显示“已确认兼容”。
5. 自动筛选优先使用已确认范围；如果可解析的官方表没有匹配候选，停止并说明原因。没有可用兼容规则时，只展示未确认候选，继续安装需要用户明确确认，默认拒绝。已明确超出范围或弃用的所选版本直接阻止安装。
6. 安装前用实际 Kubernetes 版本、集群 API 版本列表、最终 values 和相同镜像策略运行 `helm template`。检查渲染资源的 API 版本是否存在；首次安装考虑同 Chart 的 CRD，升级不把 `crds/` 内的新 CRD 当作已安装，因为 Helm 不自动升级这些文件。预渲染文件位于权限受限的临时目录，退出清理，不打印 Secret 内容。
7. 普通插件的控制器、sidecar、initContainer 和 hook 使用所选 Chart 自带的配套镜像；下载源只改仓库地址，保留 tag/digest。Envoy 数据面按下节的版本元数据自动解析。不要求用户逐个填写默认组件镜像，也不把它们分别升级到各自的 `latest`。
8. 升级使用新 Chart 默认值，再叠加已有显式 values、本次 values 和镜像源覆盖。若筛选或手工选择的 Chart 低于已安装版本，单独提示降级并默认拒绝。已有或额外 values 指定的自定义镜像仍保留，并提示用户核对；例如 Velero 云存储插件镜像等外部扩展无法由通用 Chart 范围证明兼容。业务所需的 DNS、存储、认证等配置仍由使用者提供。

安装前检查覆盖版本声明和资源 API 版本，不等于服务端 schema 校验、CRD 所有权/迁移审查、全部上游支持策略或真实功能验收；模板中的 `lookup`、API kind 级别条件与实际安装仍可能有差异。Helm/kubectl 也须适合当前集群版本。本次未执行集群安装或插件 E2E。

官方规则来源：

- [Helm Chart 的 kubeVersion](https://helm.sh/docs/topics/charts/#the-kubeversion-field)
- [Metrics Server 兼容表](https://github.com/kubernetes-sigs/metrics-server#compatibility-matrix)
- [cert-manager 支持版本](https://cert-manager.io/docs/releases/)
- [Envoy Gateway 兼容表](https://gateway.envoyproxy.io/news/releases/matrix/)
- [KEDA Kubernetes 兼容表](https://keda.sh/docs/latest/operate/cluster/)
- [Kyverno 发布支持范围](https://kyverno.io/docs/installation/releases/)

### Envoy Proxy 镜像自动解析

只需选择 Envoy Gateway Chart 版本，脚本从下载的 Chart 元数据读取 `appVersion`，自动处理数据面镜像：

1. 本次额外 values 中明确设置的 `global.images.envoyProxy.image` 或 `config.envoyGateway.envoyProxy` 容器镜像配置优先，保留高级用户的覆盖能力。
2. 没有新覆盖时，保留已有的自定义/私有镜像及显式容器配置。已有标准 `envoyproxy/envoy` 镜像随本次所选 Gateway 版本重新解析，避免升级后仍粘住上次的自动结果；若要锁住某个标准镜像，在额外 values 中明确指定。
3. 查询该版本的官方发布页面，必要时查询官方文档仓库中同名版本的发布说明。仅接受页面标题与所选版本一致、且明确写出的 `global.images.envoyProxy.image=...` 建议；矛盾或版本不符则失败，不挑一个猜测。
4. 发布说明没有明确建议或获取失败时，优先采用所选 Chart 的镜像默认值。Chart 为空时，从 `envoyproxy/gateway/<版本标签>/api/v1alpha1/shared_types.go`（兼容 `envoyproxy_types.go`）读取 `DefaultEnvoyProxyImage` 字面量，保留 tag 和 SHA256。下载或解析都没有可靠结果时停止，显示清楚的错误，不再退回手工输入坐标。
5. 元数据请求使用当前选择的文件代理；失败可尝试其他官方元数据路径，仍使用同一下载方案，不暗中绕过所选代理。上游目录或格式改变到无法识别时，应更新解析适配。最后显示实际镜像与依据，并按当前镜像源转换下载地址。

这样没有固定的 Gateway→Envoy 版本对照表，也不把 `latest` 当作兼容性推断。官方可能在版本发布后补充镜像升级建议，因此解析时查看该版本页面，而不是永久缓存上一次的推荐值。发布建议不可达而使用 Chart/源码默认值时，会打印相关下载告警；这不代表已经验证了发布后的全部安全更新。

解析依据示例：[v1.9.2 发布说明](https://gateway.envoyproxy.io/news/releases/notes/v1.9.2/) 与[该版本的源码常量](https://github.com/envoyproxy/gateway/blob/v1.9.2/api/v1alpha1/shared_types.go)。二者的镜像版本可能不同，发布说明的明确建议优先。未来版本使用自己的元数据，不复用示例版本号。

## 自定义企业代理约定

第 5 项需要填写两个值，**均为无账号密码的地址**：

```text
文件前缀：https://downloads.example.com
镜像前缀：registry.example.com/cache
```

对应关系：

```text
https://charts.jetstack.io/index.yaml
  -> https://downloads.example.com/charts.jetstack.io/index.yaml

oci://docker.io/envoyproxy/gateway-helm
  -> oci://registry.example.com/cache/docker.io/envoyproxy/gateway-helm

quay.io/prometheus/prometheus:vX.Y.Z
  -> registry.example.com/cache/quay.io/prometheus/prometheus:vX.Y.Z
```

文件代理需要实际代理上游 HTTPS 文件并保持内容；OCI/镜像代理需为相应原仓库配置缓存项目、同步或反向代理。例如一个只缓存 Docker Hub 的 Harbor 项目无法直接代理 Quay。若使用 ACR/TCR，请先同步至约定路径，不支持此路径的现有私有布局应通过插件额外 values 指定完整镜像。

私有 OCI 仓库在同一执行账号下先用 `helm registry login` 配置认证。03 中 Helm 常以 root 运行，因此登录账号也应一致。Pod 鉴权使用插件的 imagePullSecrets values；Helm 登录不会自动赋予各节点 Pod 拉取权限。使用受信 CA，不提供关闭 TLS 校验的选项。

## 作用范围与切源

- 全部 16 项插件（Loki/Alloy 为两个 release）的后续安装共用选择；`S` 切源不修改任何已有 release。已有 release 仍先询问是否升级，默认跳过。
- 后续明确升级时，会将已识别的公共镜像代理路径转回原仓库后再应用新方案，避免代理前缀不断叠加。自定义前缀历史保存在同一源配置中，跨节点换机器操作时需要携带此配置或通过额外 values 指定原始镜像。
- 未知私有 registry 域名保留原地址。脚本不能判断 `docker.io` 等公开 registry 中的某个具体仓库是否私有；若自定义 values 含 Docker Hub 私有镜像，应选择官方或企业方案，并配置相应网络和 imagePullSecrets，公共镜像站不能代替私有授权。
- 不接管 Argo CD 后续同步的业务镜像、用户新建 Gateway 中覆盖的 EnvoyProxy 镜像、cert-manager 后续 ACME solver 等二次工作负载。若需要全体业务和后续资源自动走代理，应另行配置集群运行时或对应控制器，不能用安装时的选择代替。
- 原有管理员 kubeconfig、集群基础组件、SSH/网络和数据盘不受此菜单修改。
- `scripts/addons/sources.py` 通过 `python3 scripts/sync-addon-sources.py` 同步到入口嵌入区，正常运行无需另外加载这份下载辅助源码；界面仍依赖 `locales/` 与 `scripts/lib/`，应复制完整项目。

## 排查下载失败

截图中的 `registry-1.docker.io/.../gateway-helm/manifests/...` 是 **Helm OCI Chart 下载**，尚未进入 Pod 拉镜像阶段。仅修改 containerd 的 Docker Hub mirror 不会修复 Helm 下载。

1. 在 03 主菜单确认显示的是预期来源；通过 `S` 选择 DaoCloud 等方案后再安装。
2. 查看脚本打印的下载 URL/OCI 地址；核对 DNS、证书、节点出口以及代理的白名单和限流。
3. 403/404/429 可能是未允许的包、未缓存、鉴权或限流，不代表其他插件也一定能用。切换来源或使用已同步到企业仓库的指定版本。
   若提示“镜像源跳转到未允许的域名”，核对该服务的 CDN 规则；不要直接关闭 HTTPS 校验。旧脚本的 `Mirror redirected to another host` 曾误拒绝 DaoCloud 的正常跨域 CDN，需更新完整脚本包后重跑。
4. 若 Chart 已下载但 Helm 等待失败，查看 `helm status` 和 `kubectl describe pod` 的 Events。`ImagePullBackOff` 是节点侧镜像拉取，需要检查渲染后的镜像地址和各节点到目标 registry 的连通性；不是 Helm 索引问题。
5. 出错后重跑同一脚本。若存在失败的 release，按原有流程明确选择重新配置/升级；不要直接重建集群。

## 本次验证边界

2026-10-18 在开发机读取报错的 Envoy 发布目录代理地址，确认响应为 302，目标为 `files-mirror.r2.daocloud.vip`；沿此 HTTPS 跳转读取返回 200，正文包含 67 个稳定版本链接。这是该地址当时的网络诊断结果，不代表 CP 节点出口、Chart 下载或插件安装已经验证。

本功能随 0.2.0 发布。上述网络诊断保留原始日期和范围；维护者于 2026-10-22 确认本版已在本地 Kubernetes 集群完成完整验证，见 [发布验收记录](validation/release-0.2.0.md)。本地环境的通过不保证所有公共代理持续可达；此前的 [插件 E2E 记录](validation/addons-e2e.md) 继续只说明其记录版本和环境。
