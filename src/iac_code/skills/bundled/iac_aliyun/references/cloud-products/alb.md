# ALB（应用型负载均衡）ROS 产品参考

## 适用范围与快速导航

本参考面向 `ROSTemplateFormatVersion: 2015-09-01` 模板中的 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener` 与 `ALIYUN::ALB::Rule`，用于离线模板生成、字段选择与结构校验；不适用于 CLB 或 NLB，也不验证云侧库存、权限、部署、计费或运行时状态。所有字段路径均指 ROS 模板 `Properties` 下的属性路径，不要按 API、SDK 或控制台字段名推断 ROS 层级。使用时先按导航定位最小相关章节，不要为单个字段通读无关资源或完整 schema。

| 目标 | 资源 | 关键入口 |
| --- | --- | --- |
| 创建或修改 ALB 实例 | `ALIYUN::ALB::LoadBalancer` | `AddressType`、`LoadBalancerEdition`、`ZoneMappings` |
| 创建后端服务器组 | `ALIYUN::ALB::ServerGroup` | `ServerGroupType`、`Protocol`、`HealthCheckConfig` |
| 创建监听并转发流量 | `ALIYUN::ALB::Listener` | `LoadBalancerId`、`ListenerProtocol`、`DefaultActions` |
| 在监听内增加转发规则 | `ALIYUN::ALB::Rule` | `ListenerId`、`RuleConditions`、`RuleActions` |
| 输出资源 ID 或 DNS | 已声明资源 | `Ref` 或 `Fn::GetAtt` |
| 高效生成、查证与错误修复 | 全部资源 | “高效生成、查证与错误修复”、“生成、修改与校验流程” |

- 先确定 `LoadBalancer → Listener → Rule` 的资源链路，再确定 `Listener.DefaultActions` 与 `Rule.RuleActions` 转发到哪些 `ServerGroup`；需要 ALB 与 `ServerGroup` 同 VPC 时，先锁定同一个已声明 VPC 参数。
- 私网或公网地址模式、计费与可用区先读 `LoadBalancer`；HTTP/HTTPS 监听先读 `Listener`；路径、域名或加权转发先读 `Rule`；只补输出或引用时先读“资源关系与可读属性”。
- 修改既有模板时，先记录已声明的 `Resources`、`Parameters`、资源逻辑 ID 和非目标资源；本文的 ALB 约束只作用于本次涉及资源，不得据此删除或改写用户未要求变更的既有结构。
- 生成或修改业务模板时，只补充用户要求、必填或条件必填的属性，不主动补齐可选默认值；介绍可选能力不等于默认写入。

## 高效生成、查证与错误修复

### 生成前结构映射

- 先建立结构映射：顶层键、`Resources` 中每个资源逻辑 ID 与类型、关键 `Properties`、已声明 `Parameters`、`Outputs` 键及要求的引用方式；修改既有模板时同时标出未授权改动的非目标结构。每个 `Ref` / `Fn::GetAtt` 都回查该映射，不从名称相似度、文件名或示例模板猜测，不引入示例名称或后缀。
- 先落主链 `LoadBalancer → Listener → Rule`，把 `ServerGroup` 与 `LoadBalancer` 绑定到同一个 VPC 参数，先写关联字段再填各资源必填或条件必填属性；条件字段按协议、地址模式、后端类型或目标数量分支。
- 用户限定参数清单时只声明这些参数，不新增便利参数；云资源 ID 取值通过已声明参数或资源引用传入。

### 工具选择与查证顺序

| 目标 | 优先做法 | 边界 |
| --- | --- | --- |
| JSON 语法与编码 | 本地 JSON 解析器、UTF-8 纯 JSON 检查、定向静态检查 | 不用网络、云 API、部署或询价结果替代 |
| 既有模板修改 | 只读取目标资源声明、相关参数和输出片段，保留结构 | 不通读无关资源，不改写非目标结构 |
| 字段含义与层级 | 用户明确要求 → 既有模板声明 → 本参考对应资源表 → 目标 ROS 官方 schema | 用户要求优先于本文占位符，但不得违反已确认的 ROS 语法、字段层级和引用规则；API、SDK 或控制台字段只作线索，不能直接推断 ROS `Properties` 层级 |
| 参数、输出与引用 | 先读取既有 `Parameters` / `Outputs` 与相关资源声明，再按属性表确定引用目标 | 不新增用户未要求的参数或输出，不把云资源 ID 写成字面量 |
| 引用合法性 | 逐个核对 `Ref` / `Fn::GetAtt` 的目标与属性 | 不按名称相似度、文件名或示例猜测 |
| 未覆盖字段或版本差异 | 查询目标 ROS 官方 schema 的对应资源页 | 不用 API、SDK、控制台、示例模板或相似产品推断 ROS 字段；无法确认就说明未知 |
| 云侧状态 | 不在本参考范围内 | 不验证库存、权限、部署、计费或运行时健康 |

查证优先级固定为：用户明确要求与授权范围 → 既有模板目标声明 → 本参考对应资源表 → 目标 ROS 官方 schema。前两级决定写什么值、用什么名称和引用谁；后两级确认字段层级、类型、枚举与属性合法性。

### 最小修复与重试

1. 先按失败层定位：JSON 语法或编码 → 顶层结构与授权范围 → `Ref` / `Fn::GetAtt` 与 `Outputs` → 条件字段、类型、枚举与嵌套路径 → 未确认字段。只读取错误对应的资源声明、参数或输出片段，以及本文最小相关章节。
2. 每次只做一个最小且有依据的修复；若同时涉及解析、引用与字段约束，先修复阻断解析或引用的目标问题，再修复字段层级、类型、枚举或范围。修复后先确认模板仍是 UTF-8 纯 JSON 且可解析，再复查该错误及直接受影响的引用和嵌套路径。
3. 最终交付前执行一次覆盖用户要求的完整静态检查；同一错误无新信息时，不要重复同一检查或修改，不通读无关资源，也不批量添加字段。
4. 仍无法解决时，改为核对用户要求、既有模板、本参考与目标官方 schema 的差异，并列出仍无法确认的字段清单。
5. 重试不得改变用户指定的资源逻辑 ID、参数名、名称、`Outputs` 键名或未授权的既有结构；不通过改名、硬编码云资源 ID、批量补可选字段、删除非目标资源或重写未授权区域换取校验通过。

## 引用与命名规则

- 用户明确指定的资源逻辑 ID 必须逐字保留；`Ref` 与 `Fn::GetAtt` 都必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。
- `Ref` 可以引用本模板已声明的资源、已声明的参数，或 ROS 支持的伪参数。
- `Fn::GetAtt` 只能作用于本模板已声明的资源并获取该资源类型的合法属性，语法为 `{"Fn::GetAtt": ["<资源逻辑ID>", "<属性名>"]}`；不能把参数或伪参数当作被取属性的资源。
- 两类函数的合法目标不同，必须分别核对，不得合并为同一组合法目标。
- 用户指定的参数名同样逐字保留；云资源 ID 类取值应通过参数引用传入，不要在模板中硬编码真实云资源 ID（例如 `vpc-`、`vsw-`、`lb-`、`sgp-`、`lsn-`、`rule-`、`cert-`、`eip-` 前缀的编号）。
- 名称类字段使用用户给定值；本文占位符仅用于示意结构，不能替代用户指定名称。

## 资源关系与可读属性

| 关系 | 完整字段路径 | 引用写法与条件 |
| --- | --- | --- |
| ALB 与服务器组同 VPC | `LoadBalancer.Properties.VpcId`、`ServerGroup.Properties.VpcId` | 需要同 VPC 时引用同一个已声明 VPC 参数：`{"Ref": "<VPC参数逻辑名>"}` |
| 监听关联 ALB | `Listener.Properties.LoadBalancerId` | `{"Ref": "<LoadBalancer逻辑ID>"}` |
| 规则关联监听 | `Rule.Properties.ListenerId` | `{"Ref": "<Listener逻辑ID>"}` |
| 监听默认转发 | `Listener.Properties.DefaultActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId` | `DefaultActions[].Type` 为 `ForwardGroup`，值用 `{"Ref": "<ServerGroup逻辑ID>"}` |
| 规则转发 | `Rule.Properties.RuleActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId` | `RuleActions[].Type` 为 `ForwardGroup`，值用 `{"Ref": "<ServerGroup逻辑ID>"}` |
| ALB 挂载可用区 | `LoadBalancer.Properties.ZoneMappings[].ZoneId`、`LoadBalancer.Properties.ZoneMappings[].VSwitchId` | 每个列表项分别引用对应的 Zone 与 VSwitch 参数，不要混写 |

| 资源 | 可读属性 |
| --- | --- |
| `ALIYUN::ALB::LoadBalancer` | `LoadBalancerId`、`DNSName`、`AddressType`、`LoadBalancerEdition`、`VpcId`、`ZoneMappings` |
| `ALIYUN::ALB::ServerGroup` | `ServerGroupId` |
| `ALIYUN::ALB::Listener` | `ListenerId` |
| `ALIYUN::ALB::Rule` | `RuleId`、`RuleIds` |

`DNSName` 只能通过 `Fn::GetAtt` 获取，不要用 `Ref` 或虚构属性替代；其余上表 ID 属性可用 `Ref` 或 `Fn::GetAtt`。`Outputs` 的键名、数量和取值属性应完全遵循用户要求，值必须是合法引用对象。

## LoadBalancer 常用字段

| 字段路径 | 约束 | 条件说明 |
| --- | --- | --- |
| `Properties.VpcId` | 引用已声明 VPC 参数 | 与服务器组保持同一 VPC 时引用同一参数 |
| `Properties.AddressType` | `Intranet` / `Internet` | 私网实例用 `Intranet`；公网完整配置见“未覆盖能力” |
| `Properties.LoadBalancerEdition` | `Basic` / `Standard` / `StandardWithWaf` | 按用户要求选择 |
| `Properties.LoadBalancerBillingConfig.PayType` | `PostPay` | 该配置对象在离线口径下只包含 `PayType` |
| `Properties.AddressIpVersion` | `IPv4` / `DualStack` | `DualStack` 场景需查询目标 schema |
| `Properties.AddressAllocatedMode` | `Dynamic` / `Fixed` | `AllocationId` 只在公网 `Fixed` 场景相关 |
| `Properties.ZoneMappings` | 列表至少两项；每个列表项包含 `ZoneId` 与 `VSwitchId` | 通常分别用 `Ref` 引用用户声明的 Zone/VSwitch 参数 |
| `Properties.DeletionProtectionEnabled` | 布尔值 | 不要写成字符串；用户要求显式设置时才写入 |
| `Properties.LoadBalancerName` | 2 到 128 字符 | 使用用户给定名称，不用示例名称替代 |

`ZoneMappings` 的 `ZoneId`、`VSwitchId` 与条件字段 `AllocationId` 属于列表项对象，不要提升到 `Properties` 顶层。`AllocationId` 不是通用公网带宽字段，不应在不适用的地址模式中主动添加。

## ServerGroup 常用字段

| 字段路径 | 约束 | 条件说明 |
| --- | --- | --- |
| `Properties.VpcId` | 引用与 LoadBalancer 相同的 VPC 参数 | 保持同 VPC 关系 |
| `Properties.ServerGroupType` | `Instance` / `Ip` / `Fc` | 按用户要求选择 |
| `Properties.Protocol` | `HTTP` / `HTTPS` / `gRPC` | 这是后端服务器组协议，不要与 Listener 协议混用 |
| `Properties.ServerGroupName` | 2 到 128 字符 | 使用用户给定名称 |
| `Properties.Scheduler` | `Wrr` / `Wlc` / `Sch` / `Uch` | 可选，仅在用户要求调度算法时写入 |
| `Properties.HealthCheckConfig.HealthCheckEnabled` | 布尔值 | 必填 |
| `Properties.HealthCheckConfig.HealthCheckProtocol` | 必填；本文未枚举完整取值 | 用户要求 `HTTP` 时按其要求写入；其他取值需查询目标官方 schema |
| `Properties.HealthCheckConfig.HealthCheckPath` | 字符串路径 | 必填，使用用户给定值 |
| `Properties.HealthCheckConfig.HealthCheckConnectPort` | 整数端口 | 必填，使用用户给定值 |
| `Properties.HealthCheckConfig.HealthCheckCodes` | `http_2xx` 至 `http_5xx` | 可选，仅在用户要求时写入 |
| `Properties.HealthCheckConfig.HealthCheckInterval` | 1 到 50 | 可选 |
| `Properties.HealthCheckConfig.HealthCheckTimeout` | 1 到 300 | 可选 |
| `Properties.HealthCheckConfig.HealthyThreshold` | 2 到 10 | 可选 |
| `Properties.HealthCheckConfig.UnhealthyThreshold` | 2 到 10 | 可选 |
| `Properties.HealthCheckConfig.HealthCheckMethod` | `GET` / `HEAD` | 可选 |
| `Properties.HealthCheckConfig.HealthCheckHttpVersion` | `HTTP1.0` / `HTTP1.1` | 可选 |
| `Properties.StickySessionConfig.StickySessionEnabled` | 布尔值 | 如声明会话保持，离线口径下该配置对象只包含此字段；开启场景需查询 schema |

`HealthCheckConfig` 与 `StickySessionConfig` 是 `Properties` 下彼此独立的配置对象，字段必须写入对应对象内部，不能按 API/SDK 参数名推断或平铺层级。

## Listener 常用字段

| 字段路径 | 约束 | 条件说明 |
| --- | --- | --- |
| `Properties.LoadBalancerId` | `{"Ref": "<LoadBalancer逻辑ID>"}` | 必填关联 |
| `Properties.ListenerProtocol` | `HTTP` / `HTTPS` | 按用户要求选择 |
| `Properties.ListenerPort` | 1 到 65535 的整数 | 使用用户给定端口 |
| `Properties.Certificates` | 列表；每个列表项包含 `CertificateId` | HTTPS 必填，`CertificateId` 用 `Ref` 引用用户声明参数；HTTP 不得声明 |
| `Properties.Http2Enabled` | 布尔值 | 仅 HTTPS 有效；HTTP 不得声明 |
| `Properties.DefaultActions[].Type` | `ForwardGroup` | 默认转发动作通常为一个；用户指定数量时以用户要求为准 |
| `Properties.DefaultActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId` | `{"Ref": "<ServerGroup逻辑ID>"}` | 单一默认转发目标不需要 `Weight`；多目标默认动作的权重规则未在离线来源确认 |
| `Properties.GzipEnabled` | 可选 | 仅在用户要求时写入 |
| `Properties.RequestTimeout` | 可选 | 类型与范围不在本参考确认范围，需查询目标 schema |
| `Properties.IdleTimeout` | 1 到 3600 | 可选，仅在用户要求时写入 |

`Certificates` 是列表，`CertificateId` 是列表项字段；`DefaultActions`、`ForwardGroupConfig`、`ServerGroupTuples` 依次为列表、对象、列表，不要合并或跳过层级。

## Rule 常用字段

仅在用户要求路径、域名等转发规则时创建 `ALIYUN::ALB::Rule`，并通过 `Properties.ListenerId` 关联已声明 Listener。

- `Properties.Priority`：1 到 10000 的整数，且同一 Listener 内必须唯一。
- `Properties.Direction`：`Request` 或 `Response`，按用户要求或规则语义选择。
- `Properties.RuleName`：使用用户给定名称；长度、必填性与完整字符集需查询目标官方 schema。
- `Properties.RuleConditions`：本参考确认 `Type: Path`（配置对象为 `PathConfig.Values`，路径值可使用前缀或通配匹配）与 `Type: Host`（配置对象为 `HostConfig`，完整嵌套字段需查询目标 schema）；Header 等复杂条件需查询目标 schema。
- `Properties.RuleActions`：支持 `Type: ForwardGroup`、`Redirect` 与 `Rewrite`；本文仅覆盖 `ForwardGroup` 的离线校验，其余动作需查询官方 schema。
- `ForwardGroup` 的嵌套路径为 `Properties.RuleActions[].ForwardGroupConfig.ServerGroupTuples[]`，每个列表项用 `ServerGroupId` 引用目标；若动作还要求 `Order` 或其他字段，先核对目标官方 schema，不要从字段名推断嵌套位置。
- 单一转发目标可省略 `Weight`；多个转发目标必须在每个转发目标列表项上显式给出 0 到 100 的整数 `Weight`，完整路径为 `Properties.RuleActions[].ForwardGroupConfig.ServerGroupTuples[].Weight`，`0` 表示该目标不接收流量；不要把 `Weight` 提升到动作对象或 `ForwardGroupConfig` 顶层。

## 易错差异

- `ServerGroup.Protocol` 是后端协议，`Listener.ListenerProtocol` 是前端监听协议，二者枚举不同且不可互相替代。
- `Certificates` 与 `Http2Enabled` 只适用于 HTTPS Listener；HTTP Listener 声明它们属于字段误用。
- `AllocationId` 只与公网 `Fixed` 地址场景相关，不是通用公网字段。
- `Weight` 的适用性取决于目标数量：`Rule` 多目标必须显式整数权重，单一目标可省略；不要为单一默认动作补写权重。
- `DNSName` 只能通过 `Fn::GetAtt` 获取；`Ref` 与 `Fn::GetAtt` 的合法目标不同，必须分别核对。
- 名称字段、资源逻辑 ID 和参数名都应逐字使用用户给定值；不要用本文占位符或示例名称替代。

## 生成、修改与校验流程

1. 先提取用户要求的资源清单、数量、逻辑 ID、参数名、协议与必须保留的既有模板结构；不要用示例命名改写用户命名。
2. 区分必填、条件必填与可选字段，只写入用户要求、必填或条件必填的属性；介绍可选能力不等于默认写入。
3. 按资源关系表书写 `Ref` / `Fn::GetAtt`，并保持嵌套字段路径完整。
4. 本地静态检查：文件为 UTF-8 纯 JSON、可解析；`ROSTemplateFormatVersion`、顶层键、资源类型、数量、逻辑 ID、参数与输出符合用户要求；每个 `Ref` 目标是本模板已声明的资源、已声明的参数或支持的 ROS 伪参数；每个 `Fn::GetAtt` 的资源与属性组合合法；条件字段只出现在适用场景；类型、范围、枚举正确；没有真实云资源 ID 字面量或未授权资源。
5. 字段含义、层级、版本差异或复杂组合不确定时，查询目标 ROS 官方 schema；仍无法确认时应明确说明未知，不要猜测。
6. 修复错误时按具体资源、完整属性路径和错误信息定位，并遵循“高效生成、查证与错误修复”中的最小修复与重试边界；只改动出错字段或用户授权的相邻结构，不要通过批量添加可选字段、改名资源或替换用户 ID 消除错误。

静态校验通过只说明模板结构、引用关系与本参考确认的字段约束正确，不表示库存、权限、部署、计费、运行时健康或主机验收已验证。

## 未覆盖能力与查证时机

以下能力本文仅保留枚举、关系或条件提示，未形成完整离线校验规则；模板实际要求它们时应查询目标官方 schema，并说明版本差异或未知字段：

- 公网 `Internet` 实例的完整字段组合，IPv6 或 `DualStack`，`Fixed` 地址与 `AllocationId` / EIP。
- 非 HTTP 后端协议（如 `HTTPS`、`gRPC`）的高级语义。
- `Host` / Header 条件的完整嵌套字段。
- `Redirect` 与 `Rewrite` 动作。
- 开启会话保持。
- `RequestTimeout` 的类型与范围，`RuleActions` 的 `Order` 等动作字段，以及各名称字段的完整字符集与是否必填。

若缺少必要信息，应返回需要确认的字段清单，而不是生成无法验证的结构。

## 官方来源

以下 URL 仅记录来源；离线生成与校验不需要访问网络。

- `ALIYUN::ALB::LoadBalancer`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer>
- `ALIYUN::ALB::ServerGroup`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup>
- `ALIYUN::ALB::Listener`：<https://www.alibabacloud.com/help/en/ros/developer-reference/aliyun-alb-listener>
- `ALIYUN::ALB::Rule`：<https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule>
- ROS 资源类型总表：<https://help.aliyun.com/zh/ros/developer-reference/list-of-resource-types-by-service>
