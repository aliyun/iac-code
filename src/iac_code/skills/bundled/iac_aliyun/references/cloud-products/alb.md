# 应用型负载均衡 ALB（ROS）

本文覆盖 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener` 和 `ALIYUN::ALB::Rule` 的常用原生 ROS 字段、关系和检查点。内容来自离线官方资料与资源实现摘要，不是完整 schema；未覆盖属性、能力变化、校验失败或版本差异仍需查询目标版本 schema / 文档。

## 使用入口

- 任务已明确 ALB 时直接读本页，不要先列举 `cloud-products/`、搜索 `slb.md` 或读取其他云产品文件；通用 ROS 模板规范仍按外层技能要求执行。
- 本文已覆盖的 ALB 普通字段可直接使用，不必全局搜索或重复发现；但不得因此禁止 `GetResourceType`、官方文档检索或必要校验。
- 只实现用户要求的最小必要资源与字段；未要求的可选名称、阈值、状态码、调度算法、重定向、访问控制、成员绑定或其他扩展不要添加，必填字段仍需提供。

## 通用约束

- 用户明确指定的资源逻辑 ID 必须逐字保留；`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。禁止固化测试题 ID 不代表可以忽略当前用户命名。
- 外部 VPC、可用区、交换机、证书等 ID 应参数化，不写真实 ID、伪造常量或示例 ID；用户要求参数必填时不要添加 `Default`。
- ALB 与 CLB/SLB 结构不同：不要把 `ALIYUN::SLB` 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId`、`VServerGroupId`、`BackendServerPort` 或 CLB Listener 健康检查字段套用到 ALB。
- 输出必须是对应资源的 `Ref` 或 `Fn::GetAtt`，不得写常量。模板只声明配置，不证明库存、配额、证书权限、地域可用性或运行时资源已存在。

## 既有网络参数

只引用既有 VPC、可用区和交换机时，可直接使用下表配置控制台参数关联；保留用户给定的参数名，并按用户要求保持必填且不添加 `Default`。

本表已覆盖 ALB 常用的既有网络参数 `AssociationProperty` 及联动 Metadata；不要为了复核这些网络字段再打开通用参数规范，也不要为证书等未覆盖外部 ID 打开该规范寻找产品特定属性——其高频表没有证书项。

| 参数模式 | `AssociationProperty` | `AssociationPropertyMetadata` |
|---|---|---|
| `VpcId` | `ALIYUN::ECS::VPC::VPCId` | `RegionId: ${ALIYUN::Region}` |
| `ZoneId<N>` | `ALIYUN::ECS::ZoneId` | `RegionId: ${ALIYUN::Region}` |
| `VSwitchId<N>` | `ALIYUN::VPC::VSwitch::VSwitchId` | `RegionId: ${ALIYUN::Region}`、`VpcId: ${VpcId}`、`ZoneId: ${ZoneId<N>}` |

`AssociationProperty` 只是参数元数据，不是 ALB 资源字段，不得引入额外参数、动态查询或资源字段。证书等其他外部 ID 的元数据不在本表内：允许检索时，先在目标参数规范或官方文档确认确切的属性值和 Metadata；用户禁止文档检索、网络搜索或云 API 查证且本页来源未核实时，证书参数保持用户要求的必填 `String` 并在 `Certificates[].CertificateId` 中用 `Ref` 引用，不得为满足泛化参数规则而臆造属性名。

## 资源选择与关系

| 需求 | 资源 | 关键字段与关系 |
|---|---|---|
| 创建 ALB | `ALIYUN::ALB::LoadBalancer` | `VpcId` 与 `Instance` / `Ip` 服务器组同 VPC；`ZoneMappings` 成对包含 `ZoneId` 与 `VSwitchId` |
| 后端逻辑组 | `ALIYUN::ALB::ServerGroup` | `Instance` / `Ip` 组配置 `VpcId`；健康检查在组上；成员绑定不在组顶层 |
| 前端协议和端口 | `ALIYUN::ALB::Listener` | `LoadBalancerId` 用 `Ref`；默认转发在 `DefaultActions` 内指向服务器组 |
| 路径等条件转发 | `ALIYUN::ALB::Rule` | `ListenerId` 用 `Ref`；规则动作指向服务器组 |
| 绑定具体后端 | `ALIYUN::ALB::BackendServerAttachment` | 独立资源，不是 `ServerGroup` 顶层成员功能 |

字段归属：`AddressIpVersion`、`AddressAllocatedMode`、`DeletionProtectionEnabled` 属于 LoadBalancer；`ServerGroupType`、`Protocol`、`HealthCheckConfig`、`StickySessionConfig` 属于 ServerGroup；`ListenerProtocol`、`ListenerPort`、`Certificates`、`Http2Enabled`、`DefaultActions` 属于 Listener。ServerGroup 顶层没有后端端口；显式健康检查端口写 `HealthCheckConfig.HealthCheckConnectPort`，前端端口写 `Listener.ListenerPort`。

## LoadBalancer（负载均衡实例）

必填顶层 `Properties`：`VpcId:String`、`AddressType:String`、`LoadBalancerEdition:String`、`ZoneMappings:List`、`LoadBalancerBillingConfig:Map`。

- `VpcId`：引用既有 VPC 参数或本模板网络资源，不写云资源 ID 常量。
- `AddressType`：`Intranet` / `Internet`，首字母大写；不是 CLB 小写值。
- `LoadBalancerEdition`：`Basic` / `Standard` / `StandardWithWaf`。
- `ZoneMappings`：每项必填 `ZoneId:String`、`VSwitchId:String`；通常至少两个不同可用区、每区一个交换机，但少数本地地域有单可用区例外，不得写成所有地域无条件强制两区。用户按编号给出参数时，同编号 Zone 与 VSwitch 成对。
- `LoadBalancerBillingConfig.PayType`：必填；按量付费为 `PostPay`。
- `AddressIpVersion`：可选，属于 LoadBalancer，支持 `IPv4` / `Ipv4` / `DualStack`；`IPv4` 是常用规范写法，不能声称 `Ipv4` 别名无效。
- `AddressAllocatedMode`：可选，支持 `Fixed` / `Dynamic`；不能从一个场景显式 `Dynamic` 推断所有版本默认值。
- `DeletionProtectionEnabled`：Boolean，默认 `false`；用户要求显式关闭时写 `false`。
- 私网 `AddressType="Intranet"` 不应附加 `AllocationId`、公网 EIP 或带宽包。

`Ref` 返回实例 ID；属性输出 `LoadBalancerId`、`DNSName`。`DNSName` 属于 LoadBalancer，不是 Listener。

## ServerGroup（服务器组）

必填 `ServerGroupName:String`、`HealthCheckConfig:Map`，其中 `HealthCheckConfig.HealthCheckEnabled:Boolean` 也必填。

- `VpcId`：`Instance` / `Ip` 组应配置，并与 ALB 同 VPC。
- `ServerGroupType`：`Instance` / `Ip` / `Fc`；`Fc` 后端协议仅 HTTP。
- `Protocol`：`HTTP` / `HTTPS` / `gRPC` / `GRPC`；`gRPC` 与 `GRPC` 都是当前实现接受的写法。
- `Scheduler`：`Wrr` / `Wlc` / `Sch` / `Uch`。
- `StickySessionConfig` 可选，与 `HealthCheckConfig` 并列；用户未要求或仅要求“不启用”粘性会话时省略整个配置，用户明确要求显式配置时才写字段值。粘性会话字段不得放进健康检查。
- 前后端协议兼容：HTTP 后端可对应 HTTP / HTTPS / QUIC 前端；HTTPS 后端对应 HTTPS 前端；gRPC 后端对应 HTTPS / QUIC 前端。不要把某个组合写成全部能力限制。

### HealthCheckConfig（健康检查配置）

- `HealthCheckEnabled`：Boolean，必填。
- `HealthCheckProtocol`：`HTTP` / `HTTPS` / `TCP` / `gRPC` / `GRPC`；按用户要求或后端协议选择，不把 HTTP 规则推广到所有协议。
- `HealthCheckPath`：HTTP 健康检查路径必须以 `/` 开头，按用户指定路径逐字写入。
- `HealthCheckConnectPort`：Integer `0..65535`；默认 `0` 表示使用后端成员端口，不是禁用健康检查，也不是监听端口。
- `HealthCheckCodes`：HTTP 使用 `http_2xx` 等类别列表；gRPC 使用专用状态码字符串或区间，不混用 HTTP 类别。
- `HealthCheckInterval`：Integer `1..50`；`HealthCheckTimeout`：Integer `1..300`；必须使用完整字段名，`Interval` / `Timeout` 不是别名。
- `HealthyThreshold` / `UnhealthyThreshold`：Integer，范围均为 `2..10`。

`Ref` 返回服务器组 ID；属性输出 `ServerGroupId`。

## Listener（监听）

必填 `LoadBalancerId:String`、`ListenerProtocol:String`、`ListenerPort:Integer`、`DefaultActions:List`。

- `LoadBalancerId`：用 `Ref` 指向本模板 ALB 逻辑 ID。
- `ListenerProtocol`：前端支持 `HTTP` / `HTTPS` / `QUIC`，不是 TCP 或 UDP。
- `ListenerPort`：Integer `1..65535`。
- `Certificates`：HTTPS 必须提供列表，列表项用 `CertificateId` 引用外部证书；证书应参数化，不写真实 ID。
- `Http2Enabled`：Boolean，仅 HTTPS 相关场景使用。
- 默认转发结构：`DefaultActions` 一项 `Type="ForwardGroup"`，其 `ForwardGroupConfig.ServerGroupTuples[*].ServerGroupId` 指向服务器组。当前 ROS Listener 默认动作不声明 `Weight`，不要复制 Rule 权重结构。健康检查在 ServerGroup，不是 Listener 的 CLB `HealthCheck` 字段。

`Ref` 返回监听 ID；属性输出 `ListenerId`。

## Rule（转发规则）

必填 `ListenerId`、`RuleName`、`Priority`、`RuleConditions`、`RuleActions`。

- `ListenerId`：用 `Ref` 指向本模板监听逻辑 ID。
- `RuleName`：String。
- `Priority`：Integer `1..10000`，同一监听内唯一，数值越小优先级越高。
- `Direction`：可选，`Request` / `Response`；请求路径规则未要求响应方向或显式字段时省略，需要显式方向时写 `Request`。
- 路径条件：`RuleConditions` 一项 `Type="Path"`，其 `PathConfig.Values` 为路径字符串列表。
- 转发动作：`RuleActions` 一项 `Type="ForwardGroup"`，包含 `Order:Integer`（`1..50000`）和 `ForwardGroupConfig.ServerGroupTuples`，每项 `ServerGroupId` 指向服务器组。

Rule 权重与 Listener 默认转发不同：单目标 `Weight` 可缺省（缺省 `100`）或显式 Integer `0..100`；多目标必须为每个转发组显式提供 `Weight`。不要把多目标规则用于单目标，也不要在 Listener 默认动作中复制该结构。用户要求目标组不重复时按其执行，但不要写成未获来源支持的通用 API 约束。

`Ref` 返回规则 ID；已核对属性输出为 `RuleId`。不要宣称 `RuleIds` 在所有版本可用，其他输出以目标版本 attributes schema 为准。

## 输出表达式

| 目标 | 表达式 |
|---|---|
| ALB 实例 ID | `{"Ref": "<ALB逻辑ID>"}` |
| ALB DNS 名称 | `{"Fn::GetAtt": ["<ALB逻辑ID>", "DNSName"]}` |
| 服务器组 ID | `{"Ref": "<服务器组逻辑ID>"}` |
| 监听 ID | `{"Ref": "<监听逻辑ID>"}` |
| 规则 ID | `{"Ref": "<规则逻辑ID>"}`；属性形式为 `{"Fn::GetAtt": ["<规则逻辑ID>", "RuleId"]}` |

占位逻辑 ID 代表用户指定的实际 ID，不得改用其他名称。用户声明多个服务器组但只要求一个泛称 `ServerGroupId` 输出且明确默认流量组时，输出该默认流量组；未明确时按用户指定执行，不要自行猜测。ID 类输出不要写成常量，`DNSName` 不要写成 Listener 属性。

## 生成前检查

1. 本地解析 JSON / YAML，确认 ROS 版本字段、资源结构和嵌套字段完整名没有拼写错误，尤其 `HealthCheckInterval`、`HealthCheckTimeout`、`ServerGroupTuples`、`PathConfig`。
2. 核对资源类型与数量、必填字段、枚举大小写、端口 / 阈值范围和字段归属；未要求的 VPC、交换机、ECS、EIP、后端绑定、重定向、访问控制或扩展功能不要添加。
3. 检查全部 `ZoneMappings` 的 Zone / VSwitch 对应关系，不要只抽查第一项；核对每个 `Instance` / `Ip` 服务器组与 ALB 同 VPC，且外部 ID 未写成常量。
4. 检查 HTTPS 条件、`Certificates[].CertificateId`、`Http2Enabled`，以及 Listener 默认动作不声明 `Weight`、Rule 单 / 多目标权重规则。
5. 检查所有 `Ref` / `Fn::GetAtt` 均使用用户指定逻辑 ID 并指向正确资源或参数；`DNSName` 来自 LoadBalancer。
6. 检查输出均为对应资源的 `Ref` 或 `Fn::GetAtt`；确认没有真实云 ID、未授权 `AllocationId`、CLB 字段或被用户禁止的运行时动作。
7. 用户明确禁止云 API、库存、网络搜索、预览、询价或部署时，只做上述本地静态检查；未受限制时按流程使用允许的校验和目标版本 schema 查证。本地检查不得被表述为云端校验。

## 来源

- `ALIYUN::ALB::LoadBalancer`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer>
- `ALIYUN::ALB::ServerGroup`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup>
- `ALIYUN::ALB::Listener`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener>
- `ALIYUN::ALB::Rule`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule>
- 字段与输出复核：离线官方资料及 ROS ALB 资源实现摘记。

以上来源不证明目标账号、地域、版本或资源实例当前可用。私网、Standard、HTTP 后端等说明只是适用条件下的选择，不得写成 ALB 全部能力限制。

<notes>
kept -> R1 的 ALB 原生资源体系、字段归属、协议矩阵、输出表达式和本地检查 -> 空种子基线曾全局检索、误写 `AddressIPVersion` 并在 Listener 默认动作加 `Weight`；INV / DOM / AGG 交付正确模板，说明这些规则可复用。
kept -> R2 的直接 ALB 入口、既有网络参数直查表、字段归属和全量 `ZoneMappings` 检查 -> 空参考触发重复搜索，基线为参数关联读取整份通用规范；加入映射后候选成功且减少无效读取。工作区特定编号配对被拒绝。
kept -> R3 的证书降级、中文资源标题和“未覆盖字段仍需目标版本查证” -> 禁止检索时不得臆造证书 `AssociationProperty`；未核实时保留必填 `String` 并用 `Ref`。跳过通用模板规范和整体压缩被拒绝。
kept -> R4 的完整产品覆盖与必要生成前检查 -> 多轮两场景 base / INV / DOM / AGG 均成功，跨场景压缩收益不稳定；额外读取流程和工作区特定规则被拒绝。
revised -> R5 的可选字段语义 -> HTTPS 路由 base 与 INV 在未要求扩展时省略 `StickySessionConfig` 和 `Direction` 均成功；DOM 显式写禁用值与单目标权重也成功。因此只写可选与按需语义，不规定唯一写法。
dropped -> 未获产品事实或跨场景支持的规则 -> 固化测试逻辑 ID、健康检查路径、路由路径、优先级、Order、完整模板、真实云 ID、单 AZ 无例外、目标组不重复的通用 API 约束，以及 R5 INV 在读取本页前未生效的“不要列举工作目录”入口指令。
metrics -> R1–R5 逐轮输出哈希与 `rounds.json` 记录一致且 R2–R5 输入均继承上一轮输出；两场景各阶段 `gateway_usage.unknown_requests` 均为 0。运行 token / 墙时仅用于比较发现轨迹，不代表正式效率审批；不得用末轮 terminal usage 替代全量。
</notes>
