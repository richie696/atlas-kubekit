# 持续运维核心设计

读者：维护者。目标版本：0.3.0。状态：沿用用户已确认的 Command / Adapter，按能力落地并分别验收。
产品规则主管来源为 [运维功能定义](../operations.md)，本文件仅定义实现职责与契约。

## 现有入口与复用边界

- `02 --check` / `check-cluster.py`：已有清单、SSH、配对和主机基线审计。
- `02 --repair`：已有主机基线修复；当前报告、读取与写操作耦合，先保留原路径。
- `04`：已有按角色的主机/Kubernetes/功能检查；不在新报告中复制整套规则。
- `05` / `scripts/lb/manager.py`：已有 LB 受管状态、候选校验与应用/回退。
- `scripts/lib/i18n.py`：复用语言发现、messageKey、回退和占位符校验。
- `06` 只承担新增运维交互与参数映射；新增模块在 `scripts/operations/`，先通过明确
  的采集结果适配边界对接既有脚本，再逐步抽取经行为核对的共用能力。

## 数据与执行边界

```text
Shell / CLI → 参数与身份校验 → 采集适配器 → 事实快照
事实快照 + 用户目标 → 纯计划函数 → OperationPlan
OperationPlan → 重新校验 + 操作锁 → 白名单执行器 → RunReport
RunReport → JSON / 文本 / 诊断包
```

输入和规则属于各能力模块：安全规则在 security，备份覆盖和完整性规则在 backup，
报告结构和允许输出字段在 reporting。transport 负责本机/SSH 命令、超时与错误，
不判定 sudo 是否应删除、备份是否可恢复。CLI 不能拼装任意远端 shell 作为计划动作。

## 模式候选说明

### Command：可审计的运维动作

- 位置：安全收尾、备份与后续生命周期操作的“预览后执行”用例。
- 问题：菜单回调直接调用系统命令会把确认、并发、审计和续跑判断分散到各入口。
- 设计：不可变 OperationPlan（schema_version、operation、target_fingerprint、actions、
  scope、resources、parameters），外层报告的 runId 绑定一次执行；参数、前提和恢复提示
  由各能力维护，执行器只接受明确的动作集合。
- 收益：同一份计划可预览与记录，CLI/未来界面共用执行契约。
- 防误用：不做通用任务队列，不把任意 shell 字符串序列化；不承诺每个动作都可撤销，
  高风险恢复不能通用重试。计划不含凭据，持久化后重新验证而非信任 JSON。
- 验证设计：计划无副作用、指纹变化拒绝、重复请求不重复变更、部分失败停止、输出脱敏。

### Adapter：主机采集和既有入口对接

- 位置：本机/SSH 命令结果到统一事实快照，以及现有审计结果到统一报告。
- 问题：各命令的退出码、超时、权限和错误输出语义不同，直接暴露会使规则依赖底层格式。
- 设计：窄契约返回 CommandResult/NodeFacts；报告保留稳定错误类别和是否已验证，
  不向规则层暴露 subprocess 对象、私钥内容或第三方数据结构。
- 收益：保持 SSH/本机边界与纯规则独立，便于逐步复用已有能力。
- 防误用：不创建万能 SystemManager；标准库已满足的文件/JSON 操作直接使用。
- 验证设计：本机与 SSH 的成功/失败语义一致，超时可区分，不把权限不足变为事实不存在。

## 其余 GoF 模式评估

- 创建型 Singleton、Factory Method、Abstract Factory、Builder、Prototype：当前没有
  共享连接池、多产品族或复杂对象构建需求；使用原生值对象和直接组装。
- 结构型 Bridge、Composite、Decorator、Facade、Flyweight、Proxy：本轮不存在独立维度
  继承组合、递归处理树、海量共享对象或透明访问需求；日志和锁显式编排。
- 行为型 Chain of Responsibility、Interpreter、Iterator、Mediator、Memento、Observer、
  State、Strategy、Template Method、Visitor：本轮先使用有限动作集合、普通函数、
  语言迭代与显式状态字段；不引入 DSL、事件总线、可插拔算法或继承流程框架。
  备份是有明确保护范围的领域制品，不作为通用内存对象撤销快照。

以上覆盖 23 项；采用的两项可用 Python 函数和数据类表达，不要求新增框架或复杂类层级。

## 第一批状态和失败语义

- 计划：planned；执行：running → succeeded / failed / cancelled。
- 恢复计划只表示 planned；没有实际恢复执行及检查时不得标 succeeded。
- 外部系统事实未知时标 UNVERIFIED，安全决策默认拒绝有破坏性的动作。
- 同一操作持锁覆盖采集复核、变更、写报告和清理；锁冲突不执行任何变更。
- 报告写入失败不能把变更标成未发生，输出包含“已执行但报告未持久化”的明确状态。
- 备份原子发布：私密临时目录采集 → 清单/校验 → 原子改名；失败不暴露成功制品。
- 第一批的锁覆盖本机备份变更；跨节点变更锁随 OPS-04 落地，不宣称已具备分布式协调。
- 执行错误尽可能写同一 runId 的失败报告；输出不可写时保留 runId 和明确错误。正常报告
  与恢复计划不含配置正文，不把底层命令 stderr 原样写入可分享的报告。
- 恢复默认写入独立目标目录或隔离演练节点；生产原位恢复需要专属适配和确认流程。

## 安全与兼容

- 保留 01～05 的公开行为，只有明确接线后才新增 CLI 参数。
- 06 的版本区块由 `scripts/update-version.py` 管理，正常操作使用完整项目目录。
- 不在此次编码过程中自动连接现有生产节点执行收尾、备份或恢复。
- 备份目标目录不可与源数据目录重叠；不跟随未批准的符号链接，不用未校验 tar 路径恢复。
- 新增 run/report/manifest 使用独立 schemaVersion，与工具版本分开管理。

## 第二批：统一配置与差异检查

沿用已确认的 Command/Adapter，不引入额外模式或框架。配置规则集中在
`scripts/operations/configuration.py`，配置私密存储在 `profiles.py`，事实采集继续使用
collector/transport。Shell 只消费验证后的固定字段，不执行 JSON 内的脚本。

- 配置格式为 `schemaVersion: 1` 的 JSON，含 cluster、ssh、versions、nodes；只允许已定义字段，
  拒绝重复键、明文凭据、未知协议版本、重复节点/IP、VIP 冲突及网段重叠。
- 节点角色由 lbN/cpN/wN 推导；必须有 lb1/cp1。部署适配只接受当前已锁定的组件版本，
  不把修改 JSON 视为已经支持新的 Kubernetes/Cilium 版本。
- 06 新增模板、校验、导入、导出及实际环境差异检查。配置导入只更新本机受管配置存储，
  不更改集群；实际变更仍通过其所属操作的确认入口执行。
- 配置存储使用当前账号拥有的 0700 目录和 0600 文件；同内容重复导入不重写，更新不同内容
  需要匹配旧 revision。锁覆盖读、比较和原子替换，不允许静默覆盖并发更新。
- 本机配置存储锁不等于分布式集群变更锁；第二批不宣称已实现跨协调器变更事务。
- 差异检查只采集登记节点；把缺失权限/节点不可达标成 UNVERIFIED，不能根据未采到信息
  推导“配置正确”。报告附字段级结果与只读变更计划，不自动改网络、安装或重启组件。
- 02 的新配置入口先验证完整配置，再建立固定字段默认值；仍执行 SSH/阶段一清单校验与部署确认。
  已有 cluster.conf 与导入配置不一致时拒绝继续，不把新配置默默覆盖进已部署集群。
- 02 重启续跑继续使用自己写入的 root-only cluster.conf；一次性导入文件不会成为可执行 shell 来源。
  显式提供的节点指纹及固定采集代码在校验后原子写入独立标记块，重跑不重复追加；
  已保存 pin 不允许被导入配置替换。输入 JSON 无法提供该段代码。

## 当前模块与状态归属

历史批次的限制说明保留其当时范围；当前实现按下表组织，不把后续能力堆进入口脚本。

| 边界 | 模块 | 拥有的规则与效果 |
| --- | --- | --- |
| Shell 部署协调 | `scripts/deploy/` | 配置映射、配对、物料一致性、固定阶段调用、原生验收；source 只定义函数 |
| 主机共用机制 | `scripts/lib/` | 国际化、身份/网络、包锁、组件默认版本和主机互斥；不决定升级/退出等业务策略 |
| 运维入口 | `scripts/operations/arguments.py`、`navigation.py`、`commands/` | CLI/菜单映射及分组调用；复用同一参数和执行契约 |
| 计划与集群协调 | `changes/`、`configuration_cluster.py` | 不可变计划、观察指纹、有效期、CAS owner/resourceVersion、执行日志及继续计划 |
| 主机技术适配 | `adapters/`、`host_actions/` | 经校验 SSH、固定代码包、动作白名单、私密进程日志及受限主机效果；无任意命令计划 DSL |
| 节点生命周期 | `lifecycle/` | 维护归属、PDB/本地数据/存储/仲裁、worker 加入退出替换、串行 Kubernetes 补丁、CoreDNS 布局恢复 |
| 组件生命周期 | `components/` | Cilium Chart/镜像/策略、逐节点驱逐、恢复原 Helm/API 更新策略；不承诺 CRD 自动降级 |
| 交付物 | `materials/` | 包依赖闭包、签名 APT、OCI/Chart/工具/源码完整性与单 ID 激活；不自动切换已有物料版本 |
| 备份与证据 | `backup*`、`recovery*.py`、`protection/` | 控制面加密备份、完整性、隔离演练和外部制品归属；不把元数据登记当作应用恢复 |
| 报告与诊断 | `reporting.py`、`native_reports.py`、`diagnostics/` | JSON/文本协议、02/04 摘要适配、白名单采集/体积/哈希，排除原始特权输出 |
| LB 管理 | `scripts/lb/{manager,remote,files}.py` | 交互与跨主机协调、候选/发布/回退、文件原子替换；与 02/06 共用主机锁 |

集群变更锁在 Kubernetes API 中以 CAS 持久化；进程崩溃不自动清除所有权，确认旧协调器和
远端作业停止后才按 owner/resourceVersion 恢复。新的继续计划重新观察事实，继承失败计划
拥有的维护或 rollout 标记，避免替管理员解除原有维护状态。

LB 跨 RPC 事务除运行时 flock 外，还在 `/var/lib/atlas-kubekit/` 保存持久预留与原/候选配置。
预留覆盖各次 prepare/commit/recover 间隙及重启；部分发布只接受旧/候选文件的混合状态。
回退/完成验证成功后先释放预留，再删除事务日志；不能先删唯一恢复证据。

Kubernetes 补丁在每个控制面恢复调度后立即恢复原 CoreDNS 副本与放置策略，再检查工作负载
健康并进入下一节点，避免 kubeadm 默认布局令后续 PDB 无预算。失败继续计划发现已完成首控制面
的 DNS 漂移时可记录暂缓的 PDB 预检；执行先修复该布局，实际驱逐前仍重新检查，不绕过 PDB。

模块使用标准库、普通函数与不可变数据类，不引入额外框架。副作用保持在适配器、主机动作
和明确编排边界；规则回归、真实基础设施验证、制品验收与资源清理分别保留证据。
