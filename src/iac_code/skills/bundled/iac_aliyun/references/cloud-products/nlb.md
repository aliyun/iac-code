# 阿里云 NLB ROS 产品参考

本参考面向阿里云 ROS 模板中的 `ALIYUN::NLB::LoadBalancer`、`ALIYUN::NLB::ServerGroup` 和 `ALIYUN::NLB::Listener`，整理资源关系、常用字段、条件约束、易错差异和交付前自查要点。字段事实来自 2026-07-23 冻结的官方 ROS 文档索引与受控 metadata；本页不是完整 schema，未覆盖字段、目标地域差异和版本差异需查目标官方 ROS 文档或目标环境 schema。

## 快速导航

| 需要解决的问题 | 优先阅读 |
|---|---|
| 任务是否需要 NLB、需要哪些资源、按什么顺序建模 | 资源选择、工具与查询顺序 |
| 快速定位某个字段属于哪个资源或哪个章节 | 常用字段索引 |
| 用户契约、逻辑 ID、参数或输出是否可改动 | 通用模板约束 |
| 资源之间如何引用，`Ref` / `Fn::GetAtt` 应指向哪里 | 资源关系 |
| TLS 终止、TCP/UDP/TCPSSL 配对、监听端口 | Listener（监听器） |
| 健康检查字段、HTTP 与 TCP 分支 | 健康检查 |
| 字段拼写、类型、枚举、范围或 ALB/NLB 差异 | LoadBalancer（负载均衡实例）、ServerGroup（后端服务器组）、易错差异 |
| 应先查本页、本地 schema 还是官方 schema，选择哪些本地工具 | 资源选择、工具与查询顺序 |
| 校验失败如何分类、最小修复、何时重试、何时查询外部 schema | 模板自查、本地校验与重试 |
| 本地校验通过后仍不能证明哪些云端事项 | 模板自查、本地校验与重试 |
| 确认官方文档入口 | 官方来源 |

导航使用规则：先按问题类型查“快速导航”或“常用字段索引”，进入最窄的相关章节；只有该章节缺少字段或分支结论时，才补读必要的另一章节。交付前统一检查时才阅读“模板自查、本地校验与重试”。不要顺序通读整页，也不要为单个字段读取全部官方文档或全部本地 schema。

## 常用字段索引

| 字段或结构 | 所属资源 | 优先阅读 |
|---|---|---|
| `VpcId` | `ALIYUN::NLB::LoadBalancer`、`ALIYUN::NLB::ServerGroup` | LoadBalancer（负载均衡实例）、ServerGroup（后端服务器组） |
| `AddressType`、`AddressIpVersion`、`ZoneMappings` | `ALIYUN::NLB::LoadBalancer` | LoadBalancer（负载均衡实例） |
| `LoadBalancerType`、`LoadBalancerBillingConfig`、`DeletionProtectionConfig` | `ALIYUN::NLB::LoadBalancer` | LoadBalancer（负载均衡实例） |
| `ServerGroupType`、`Protocol`、`AddressIPVersion`、`Servers` | `ALIYUN::NLB::ServerGroup` | ServerGroup（后端服务器组） |
| `HealthCheckConfig` 及其健康检查字段 | `ALIYUN::NLB::ServerGroup` | 健康检查 |
| `ListenerProtocol`、`ListenerPort`、`CertificateIds` | `ALIYUN::NLB::Listener` | Listener（监听器） |
| `LoadBalancerId`、`ServerGroupId` | `ALIYUN::NLB::Listener` | 资源关系、Listener（监听器） |

## 资源选择、工具与查询顺序

1. 先读取用户需求并固定契约：资源逻辑 ID、资源类型、参数名与必填状态、网络形态、可用区与交换机来源、监听协议与端口、后端形式、健康检查分支、必须保留或必须省略的能力。契约不清时不要用产品默认场景代替用户要求。
2. 选择最小资源集：入口负载均衡用 `ALIYUN::NLB::LoadBalancer`；需要后端组或健康检查时增加 `ALIYUN::NLB::ServerGroup`；需要监听入口时增加 `ALIYUN::NLB::Listener`。不要为展示产品能力添加未要求的资源、后端成员、绑定或扩展属性。
3. 建模顺序建议为：先确定 LoadBalancer 的 VPC、地址类型、IP 版本与可用区映射；再确定 ServerGroup 的 VPC、类型、协议与健康检查；最后让 Listener 直接引用前两者。该顺序只用于减少返工，不是模板 JSON 内的资源声明顺序要求。
4. 字段与工具查询顺序：
   - 修改既有模板时，先读取用户契约、被指出的资源块和引用目标；不要为单个错误重排或通读整个模板。
   - 查字段归属和语义时，先查“常用字段索引”，再读对应资源章节及“易错差异”，确认字段属于 LoadBalancer、ServerGroup 还是 Listener，以及是否处于 HTTP / TCP、TLS / UDP 或单端口 / 端口范围分支。
   - 查精确名称、类型、枚举或范围时，使用本地 `iac-aliyun` 技能已暴露的 ROS schema/metadata，并只读取当前资源、属性或错误涉及的片段。
   - 本页与本地 schema 都不足、二者冲突、或字段具有目标地域/版本敏感性时，再查询目标官方 ROS 文档或目标环境 schema。冲突时以目标环境 schema 为准，用官方文档核对语义；仍不一致时记录未知，不要择便猜字段。
5. 工具选择：
   - JSON 模板生成或修改使用能保留用户逻辑 ID、参数名、输出名和字符串值的本地文本或 JSON 处理方式；不要使用会隐式改写标识、重排无关内容或自动“美化”契约的编辑方式。
   - 语法检查先用本地 JSON 解析；结构与字段约束再用项目内置 ROS 离线 schema 校验。两项检查的作用不同，前一项通过不能替代后一项。
   - 定位问题时使用有界的局部读取、资源名或字段名检索以及修改前后差异检查；不要因一个字段错误读取无关资源或扩展产品能力。
   - 不要用云 API、库存查询、网络搜索、预览、询价或部署来替代这些本地步骤，除非用户明确授权并允许相应网络与权限。也不要凭 ALB 经验类推 NLB 字段。

## 通用模板约束

- 用户明确指定的资源逻辑 ID 是模板契约，必须逐字保留。`Ref` 与 `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加前后缀或用示例名称替代；也不要把任何具体场景的逻辑 ID 固化为产品通用要求。
- 外部 VPC、可用区、交换机和证书标识应按用户契约参数化或使用用户提供的引用，不要在通用模板中写入真实云资源 ID、证书 ID 或凭据值。
- 修改既有模板时，保留未获授权改动的 `Parameters`、`Outputs`、`Metadata`、无关资源和既有属性值；修正应集中在导致错误的具体字段。
- 用户要求省略的能力不要隐式添加；用户明确要求的能力不要为简化模板而删除。
- 参数的默认值、必填状态和允许值属于用户契约；修正字段时不要顺手改变这些约束。

## 资源关系

| 资源 | 关键关系 | 可用输出 |
|---|---|---|
| `ALIYUN::NLB::LoadBalancer` | `VpcId` 指定 VPC；`ZoneMappings` 的每一项绑定一个可用区与该可用区下的交换机。 | `Ref` 返回 `LoadBalancerId`；`Fn::GetAtt` 可读取 `DNSName`。 |
| `ALIYUN::NLB::ServerGroup` | `VpcId` 声明所属 VPC；同 VPC 部署应与 LoadBalancer 使用同一 VPC 输入。后端成员 `Servers` 是 ServerGroup 的嵌套列表，不是 Listener 字段。 | `Ref` 返回 `ServerGroupId`。 |
| `ALIYUN::NLB::Listener` | 通过 `LoadBalancerId` 和 `ServerGroupId` 直接引用模板内声明的 LoadBalancer 与 ServerGroup；不使用 ALB 的 `DefaultActions` 转发结构。 | `Ref` 返回 `ListenerId`；`Fn::GetAtt` 可读取 `ListenerPort`。 |

常见结构是：外部输入的 VPC、可用区和交换机进入 LoadBalancer 的 `ZoneMappings`；同 VPC 的 ServerGroup 作为 Listener 的直接目标；`Outputs` 用 `Ref` 输出资源 ID，用 `Fn::GetAtt` 输出 `DNSName`。

## LoadBalancer（负载均衡实例）

来源：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-loadbalancer>

- 必填：`VpcId: String`、`AddressType: String`、`ZoneMappings: List`。
- `AddressType` 枚举为 `Internet` / `Intranet`；`AddressIpVersion` 枚举为 `Ipv4` / `DualStack`。大小写必须逐字符核对，不要从 ALB 字段复制。
- `LoadBalancerType` 使用 `network`；`LoadBalancerBillingConfig.PayType` 此版本可用 `PostPay`。
- `ZoneMappings` 每项必填 `ZoneId` 与 `VSwitchId`，交换机应属于对应可用区和 VPC。常规部署应优先考虑多可用区；冻结资料中同时存在旧 metadata 的 `2..3` 与本地 runtime 的 `1..10` 记录，且少数地域可能只有一个可用区。用户未指定数量时，应按目标地域与 schema 版本核对允许范围；用户明确指定数量时，逐字保留该契约，不要把某个模板的映射数量推广为产品全局限制。
- `DeletionProtectionConfig.Enabled` 是 `String`，使用字符串形式的 `"true"` / `"false"`；不要写成 ALB 的 `DeletionProtectionEnabled: Boolean`。
- `CrossZoneEnabled` 与 `TrafficAffinityEnabled` 是 `Boolean`，不要与删除保护字段的字符串类型混用。
- `Intranet` 场景通常不配置公网 EIP、`AllocationId` 或 `BandwidthPackageId`；`Internet` 场景的附加网络字段应按需求与目标 schema 确认。
- `Ref` 返回 `LoadBalancerId`；`DNSName` 必须从 LoadBalancer 资源用 `Fn::GetAtt` 读取。

## ServerGroup（后端服务器组）

来源：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-servergroup>

- 必填：`VpcId: String`、`ServerGroupName: String`。
- `ServerGroupType` 支持 `Instance` / `Ip`；`Protocol` 支持 `TCP` / `UDP` / `TCPSSL`；`Scheduler` 枚举为 `Rr` / `Wrr` / `Sch` / `Tch` / `Qch`。
- 注意拼写差异：LoadBalancer 使用 `AddressIpVersion`，ServerGroup 使用 `AddressIPVersion`，不能互换。
- `AnyPortEnabled`、`PreserveClientIpEnabled`、`PersistenceEnabled` 是 `Boolean`；仅在需求明确时配置，不要因字段存在而默认开启。
- `Servers` 是嵌套列表；是否声明由需求决定。用户要求空服务器组时，不要额外创建后端资源或绑定。
- 较新 runtime 资料还记录了 `ServerGroupTuples` 多服务器组能力；目标 schema 未确认前不要与单目标 `ServerGroupId` 结构混用，也不能断言 NLB 永远只支持单服务器组。

## 健康检查

健康检查配置位于 `ALIYUN::NLB::ServerGroup.HealthCheckConfig`，不是 Listener 属性。

- `HealthCheckEnabled` 是 `Boolean`；显式开启或关闭时不要写成字符串。
- `HealthCheckType` 枚举为 `Tcp` / `Http`。
- HTTP 检查使用 `HealthCheckUrl`、`HttpCheckMethod`、`HealthCheckConnectPort`，不要替换为 ALB 的 `HealthCheckProtocol`、`HealthCheckPath` 或 `HealthCheckMethod`。
- 冻结 metadata 将 `HttpCheckMethod` 标为 `Required`；配置 HTTP 检查时应显式写出方法。该要求不适用于 TCP 检查，不能据此声称 TCP 检查也必须配置 HTTP 方法。
- `HealthCheckConnectPort: Integer` 范围为 `0..65535`；`0` 表示使用后端端口的默认语义，是有效输入。用户要求固定检查端口时应显式提供相应值。
- `HealthCheckConnectTimeout: Integer` 范围为 `1..300`；`HealthCheckInterval: Integer` 范围为 `1..50`；`HealthyThreshold` 与 `UnhealthyThreshold` 均为 `Integer`，范围为 `2..10`。
- `HealthCheckHttpCode` 是 String 列表；`HealthCheckDomain` 是 `String`。

## Listener（监听器）

来源：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-listener>

- 必填：`ListenerProtocol`、`LoadBalancerId`、`ServerGroupId`。单端口模板还必须明确 `ListenerPort`。
- `ListenerProtocol` 支持 `TCP` / `UDP` / `TCPSSL`。
- 单端口监听使用 `ListenerPort: Integer`，范围为 `1..65535`。此版本还支持 `ListenerPort: 0` 配合 `StartPort` / `EndPort` 表示端口范围；使用范围能力前应核对目标 schema，不要把 `0` 写成永久非法值，也不要未加核对地混用单端口与范围字段。
- `LoadBalancerId` 与 `ServerGroupId` 应通过 `Ref` 指向模板中已声明的资源，不要硬编码生成后的云资源 ID。
- `Enable` 与 `ProxyProtocolEnabled` 是 `Boolean`；`IdleTimeout: Integer` 范围为 `1..900`。
- TCPSSL 在监听端终止 TLS 且后端为普通 TCP 服务时，ServerGroup 的 `Protocol` 应选择 `TCP`。`CertificateIds` 是证书 ID 字符串列表，应引用外部必填证书参数或用户提供的值，不要使用 ALB 的 `Certificates` 对象列表。
- UDP 监听通常与 UDP ServerGroup 配对；若需求明确要求其他后端协议或 TLS 穿透，应按需求与目标 schema 配置。

## 易错差异

- NLB Listener 没有 ALB `DefaultActions`；转发目标由 `LoadBalancerId` 与 `ServerGroupId` 直接表达。
- `DeletionProtectionConfig.Enabled` 是 String；`CrossZoneEnabled`、`TrafficAffinityEnabled`、ServerGroup 开关和健康检查开关是 Boolean。
- `AddressIpVersion` 与 `AddressIPVersion` 分别属于 LoadBalancer 和 ServerGroup，拼写和大小写不同。
- NLB HTTP 健康检查字段与 ALB 不同，尤其 URL 与方法字段不要混写。
- TCPSSL 的 `CertificateIds` 是字符串 ID 列表，不是 ALB `Certificates` 对象列表。
- 可用区映射数量、多服务器组、端口范围监听具有版本或地域相关性，不要把单一场景选择推广为产品全局限制。

## 模板自查、本地校验与重试

校验顺序：先执行本地 JSON 解析，再执行项目内置离线 schema 校验；若该 schema 校验在当前环境不可用，应明确说明未执行，而不是用 JSON 解析替代。两者都通过后，只做与用户契约和易错差异相关的针对性复核。JSON 解析通过不等于 schema 通过，schema 通过也不等于云侧可部署；不要在未定位根因前反复重跑整轮校验。

1. 核对用户指定的逻辑 ID、参数名、输出名和属性值是否逐字保留；未经授权的内容不要改动。
2. 检查每个 `Ref` 目标已声明且返回所需 ID；检查 `Fn::GetAtt` 的资源类型与属性匹配，例如 `DNSName` 只能来自 `ALIYUN::NLB::LoadBalancer`。
3. 检查外部 VPC、可用区、交换机与证书是否按用户契约参数化或引用，并保留必填状态、默认值和允许值。
4. 核对字段名拼写、枚举大小写、`String` / `Boolean` / `Integer` / 列表类型、Integer 范围和嵌套位置；布尔值不要加引号，字符串化布尔值不要写成裸布尔；同时核对协议配对和 HTTP / TCP 健康检查分支。
5. 执行本地 JSON 解析和项目内置离线 schema 校验；若后一项在当前环境不可用，明确说明未执行。JSON 解析通过只证明语法有效；静态校验通过仅证明模板结构与受控 schema 合规，不证明库存、配额、权限、证书有效性、部署或运行时健康。

### 错误分诊与重试

先阅读完整错误并判断类别，再做一次最小修正；不要同时修改字段名、嵌套位置、协议分支、参数和输出。修正后先核对改动是否只涉及被指出的内容，再重跑相关本地校验。

- JSON 解析错误：按报错行号或字符位置检查括号、逗号、引号和转义，只修正语法问题；语法修复后如涉及字段结构，再运行离线 schema 校验。
- 字段不存在、枚举不符、类型或范围错误：查对应资源章节、健康检查、易错差异和相关本地 schema；保持嵌套位置，只修正被指出的字段。
- `Ref` / `Fn::GetAtt` 解析失败：查资源关系、用户契约和输出属性；修正引用目标或属性名，不要改用户指定的逻辑 ID。
- 协议、TLS、UDP 或健康检查分支冲突：查 ServerGroup、Listener、健康检查和易错差异；使用 NLB 字段与正确归属，不要换成 ALB 结构。
- 参数、输出或无关资源被改动：查通用模板约束和最近一次差异；恢复未授权改动，只保留必要的最小修正。
- 同一错误再现时停止同型重试，逐项核对拼写、大小写、类型、枚举、嵌套位置、用户契约和本地 schema；新错误先判断是否由上一次修正引入，必要时先回退该修正再重新定位。
- 本页未覆盖、页面与 schema 冲突、地域或版本敏感、官方语义不明确，或最小修正后仍失败时，查询目标官方 ROS 文档或目标环境 schema；仍无法确认时明确说明未知。不要移除校验、放宽类型、改写需求、删除输出或换用 ALB 字段；只报告实际执行的检查，未执行的云侧检查按未知表述。

## 官方来源

- [ALIYUN::NLB::LoadBalancer](https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-loadbalancer)
- [ALIYUN::NLB::ServerGroup](https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-servergroup)
- [ALIYUN::NLB::Listener](https://help.aliyun.com/zh/ros/developer-reference/aliyun-nlb-listener)
