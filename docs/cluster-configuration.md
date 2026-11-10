# 统一集群配置

适用版本：0.3.0。
统一配置描述管理员的目标，供重复部署输入和只读差异检查使用。导入配置只改变本机配置存储，
不会扩缩容、升级组件或修改远端网络。功能要求见 [持续运维功能](operations.md)，
入口及报告说明见 [运维指南](operations-guide.md)。

## 生成和校验

在完整项目目录运行，管理机需要 Python 3.9+；02 部署仍要求目标 Ubuntu 环境与 Bash 4+。
目录归当前账号所有，权限 0700，文件 0600；已存在的导出目标不会被覆盖。

```bash
umask 077
mkdir -p "$HOME/atlas-cluster"
chmod 700 "$HOME/atlas-cluster"
bash ./06-maintain-cluster.sh --lang zh_CN --config-template \
  --profile-name my-cluster --export-file "$HOME/atlas-cluster/cluster.json"

# 编辑模板中的地址、网卡及完整节点清单，再执行：
bash ./06-maintain-cluster.sh --lang zh_CN --config-check \
  --config "$HOME/atlas-cluster/cluster.json"
```

模板中的 `192.0.2.*` 为文档示例地址，必须替换。模板只有 lb1/cp1，便于阅读；
生产环境需要登记所有 LB、控制面和工作节点。`standard` 不代表这个最小模板已具备高可用。

```json
{
  "schemaVersion": 1,
  "cluster": {
    "name": "my-cluster",
    "profile": "standard",
    "apiVIP": "192.0.2.10",
    "apiPort": 6443,
    "appVIP": "",
    "podCIDR": "10.244.0.0/16",
    "serviceCIDR": "10.96.0.0/12",
    "vrrpID": 51,
    "statsAllowedCIDR": "",
    "localRegistryHost": ""
  },
  "ssh": {"user": "ubuntu"},
  "versions": {"kubernetes": "v1.36.5", "cilium": "1.20.2"},
  "nodes": [
    {"name": "lb1", "address": "192.0.2.11", "interface": "enp6s18", "dataDevice": "", "identityFingerprint": ""},
    {"name": "cp1", "address": "192.0.2.12", "interface": "enp6s18", "dataDevice": "", "identityFingerprint": ""}
  ]
}
```

## 字段与约束

| 字段 | 用途与约束 |
| --- | --- |
| schemaVersion | 配置协议版本，目前只支持整数 1；与工具版本分开 |
| cluster.name | 以小写字母开头，后接小写字母、数字或连字符，最多 63 字符 |
| cluster.profile | standard/test，沿用 02 的资源策略 |
| apiVIP/apiPort | 控制面 IPv4 VIP 和端口；端口 1～65535，不能占用 stats 的 7000 |
| appVIP | 可选应用 IPv4 VIP，与 API VIP、节点地址不同 |
| podCIDR/serviceCIDR | 规范 IPv4 CIDR，两者不重叠，节点及 VIP 不能位于其中 |
| vrrpID | 1～255；跨实例冲突仍需由现场网络规划确认 |
| statsAllowedCIDR | 空值表示仅回环监听；非空为管理 IPv4 CIDR，禁止 /0 |
| localRegistryHost | 可选固定仓库地址，格式为 IPv4/DNS:port；不接受 URL 或凭据 |
| ssh.user | 已存在的非 root 登录账号；不包含密码、私钥正文或任意远端命令 |
| versions | 记录 Kubernetes/Cilium 目标版本；02 目前仅部署 v1.36.5/1.20.2 |
| nodes[].name | lbN/cpN/wN，N ≥ 1；必须包含 lb1/cp1，不能重名 |
| nodes[].address/interface | 唯一 IPv4 及目标网卡名；部署时还会核对阶段一清单和实际地址 |
| nodes[].dataDevice | 空值沿用存储预检和交互；K8s 节点可用 none/existing、/dev/... 或 reuse:/挂载路径；LB 仅空值或 none |
| nodes[].identityFingerprint | 可选 64 位小写十六进制 SHA-256，绑定 hostname 与 machine-id；需通过可信管理通道核实 |

不接受未知字段、重复 JSON 键、错误类型或节点/VIP 冲突。文件不能通过符号链接读取。
所有节点使用同一个 SSH 用户，允许多个网卡，但每个节点只声明一个部署网卡。
更换配置内的组件版本可用于目标记录和差异检查，会产生兼容性 WARN；不会自动支持新版本安装。

## 导入、导出和并发保护

```bash
bash ./06-maintain-cluster.sh --lang zh_CN --config-import \
  --config "$HOME/atlas-cluster/cluster.json"

bash ./06-maintain-cluster.sh --lang zh_CN --config-export \
  --profile-name my-cluster --export-file "$HOME/atlas-cluster/exported.json"
```

默认存储在执行账号的 `~/.local/state/atlas-kubekit/configurations/`，可用 `--state-dir` 指定
另一个私密目录。它不会自动同步到其他管理机或节点，也不会自动读取已有 cluster.conf。
以 cluster.name 为默认配置名称；导入时可用 `--profile-name` 指定本地别名。

规范化内容计算得到 revision：字段顺序及节点排列不影响它，实际值变化会产生新 revision。
同内容再次导入不重写文件。更新不同内容时，需要使用最近导入报告或存储信封中的当前 revision：

```bash
bash ./06-maintain-cluster.sh --lang zh_CN --config-import \
  --config "$HOME/atlas-cluster/cluster.json" \
  --expected-revision '替换为当前64位revision'
```

读取、比较 revision 和原子写入在同一个本机配置锁内进行。过期 revision、锁冲突、被修改的
存储内容或宽松权限会被拒绝。此锁不协调不同管理机上的集群变更；导入成功也不表示远端已应用。

## 差异检查和变更预览

```bash
bash ./06-maintain-cluster.sh --lang zh_CN --config-drift \
  --profile-name my-cluster --sudo --ssh-identity "$HOME/.ssh/id_ed25519" \
  --output-dir "$HOME/atlas-cluster/reports"
```

也可用 `--config FILE` 直接检查。此时不要同时给 `--profile-name`；`--target` 不适用于配置操作。
通过可信 known_hosts 连接配置内节点，最多并行采集 4 台，使用固定的只读采集代码。
需要免交互 sudo 读取受限文件；账号权限不足、命令失败或不识别的定制布局均保留未验证状态。

实际核对 hostname、角色、IP/网卡、固定身份、角色服务、kubelet/Cilium 版本、Pod/Service CIDR、
LB API 转发目标、VRRP 参数、VIP 配置与持有者及 stats 来源策略。保存的 profile、SSH 用户和
仓库意图单独核对。旧节点没有 cluster.conf 时，这三项每节点标记 UNVERIFIED。

未填写身份指纹时显示 WARN 及采集到的候选指纹，管理员核实后再填入配置。
一台 LB 不可达时，无法判断全部 LB 的 VIP 唯一性，不能报告 PASS。
dataDevice 对应的真实设备/挂载和数据安全、containerd 仓库配置及仓库连通性尚未在此检查验证，
有相关声明时明确标记 UNVERIFIED。采集不是跨节点事务快照，故障切换中的短暂状态需复查。

JSON/文本报告包含 revision、字段差异及只读计划。`planExecution: not-executed` 表明没有应用变更。
退出 0 表示已覆盖检查无异常，1 表示失败/实际差异，2 表示 WARN/UNVERIFIED；不能把 2 直接当作部署失败。
报告含内部 IP、用户名和身份指纹，分享前需审核；不会收集私钥、auth_pass 或 kubeconfig 正文。

## 与 02 部署对接

先只校验输入，不需要 root，不连接节点或写阶段状态：

```bash
bash ./02-deploy-cluster.sh --lang zh_CN --config-check "$HOME/atlas-cluster/cluster.json"
```

实际部署需要把完整源码包及配置带到已完成阶段一的发起节点，节点 SSH 访问应事先登记并验证。
配置输入不替代配对/互信流程，也不会为陌生地址自动建立可信身份。普通交互部署的登记流程继续保留。

```bash
sudo bash ./02-deploy-cluster.sh --lang zh_CN --config /安全路径/cluster.json
```

02 从校验器读取固定字段，JSON 不会被 source/eval。新装仍需交互终端、阶段一清单、
实际存储预检以及 `DEPLOY ALL` 确认。已有 root-only cluster.conf 时，配置必须与保存的节点清单、
地址、参数及已选设备一致；此入口不能用来给已运行集群改 IP、替换节点、升级或改存储。
环境变量 LOCAL_REGISTRY_HOST 与配置冲突也会拒绝，不允许静默覆盖。

明确提供的身份指纹经核对后，连同工具生成的固定身份采集代码，原子保存到 02 自己维护的
root-only cluster.conf；自动续跑会重新核对这些约束，不依赖外部 JSON 文件仍在原路径。
已有 pin 不会被新配置静默替换，固定身份的节点也不会被自动改写 machine-id。
配置中未声明的应用数据和凭据不会因此加入通用配置。

第二批验证只读配置链路及 02 校验/冲突/身份持久化边界；后续已在可销毁集群执行
`02 --config` 首次安装和只读重跑，并完成跨管理机 CAS/SIGKILL/显式恢复、worker 生命周期
和 Cilium 补丁验收。受控 apply 仅接受专属能力，不会把任意 JSON 差异自动应用到集群。
Kubernetes 升级及最新冻结基线的最终状态见 [执行记录](validation/operations-e2e-20261110-execution.md)。
