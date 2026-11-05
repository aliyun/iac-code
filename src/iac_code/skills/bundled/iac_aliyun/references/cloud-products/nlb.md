# 阿里云 NLB（网络型负载均衡）ROS 模板参考

## 快速导航

- 新建模板：「使用边界与生成原则」→「资源关系」→任务涉及资源章节→「引用、参数与输出」→「本地校验与修复重试」；不要通读无关产品文档、完整 schema 或原始运行记录。
- 修改或纠错：先按「本地校验与修复重试」定位症状，再只读出错资源的相关字段；不要全文重写或盲目重试。
- 地址、可用区、计费、删除保护：「负载均衡 `ALIYUN::NLB::LoadBalancer`」。
- 服务器组、协议、后端、健康检查：「服务器组 `ALIYUN::NLB::ServerGroup`」；健康检查不在 `Listener` 下。
- 监听协议、证书、端口、关联：「监听 `ALIYUN::NLB::Listener`」。
- 大小写、类型、条件差异：「条件与易错差异」；未覆盖字段、版本差异或冲突：「未覆盖能力与官方来源」。

## 使用边界与生成原则

- 本文只覆盖 `ALIYUN::NLB::LoadBalancer`、`ALIYUN::NLB::ServerGroup`、`ALIYUN::NLB::Listener` 的离线 ROS 模板生成与校验；离线任务不调用云 API，本文不表示权限、库存、部署、计费或运行时健康已验证。
- 开始前提取模板版本、允许资源类型、精确逻辑 ID、参数约束、输出契约、必填/禁止字段和未授权改动范围；字段查找顺序为用户契约 → 本文对应资源表 → 目标环境官方 ROS schema。
- 本文已覆盖字段直接使用完整路径、类型、枚举、范围和条件；不为确认它们读取完整 schema、API/SDK 文档或无关产品资料。未覆盖、版本不明或与目标 schema 冲突时，只查询该字段的官方 ROS schema；无法确认时明确说明未确认项。
- 不要凭 API/SDK 参数名推断 ROS 层级；先区分资源 `Properties`、配置对象和列表项，再使用完整属性路径。
- 业务模板仅写入用户要求、必填或条件必填属性；可选能力必须说明启用条件，不为完整性补齐可选默认值。
- 用户明确指定的资源逻辑 ID 是模板契约，必须逐字保留；`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得改名、添加后缀或用示例名称替代。
- `Ref` 可引用已声明资源、已声明参数或支持的 ROS 伪参数；`Fn::GetAtt` 只能对已声明资源取合法属性，不能把参数或伪参数当作被取属性的资源；两类目标分别核对。
- VPC、可用区、交换机、证书等环境标识由参数或既有引用传入，不写真实云资源 ID；新增输入通常使用无 `Default` 的必填 `String`。
- 修改既有模板时只处理本次涉及的 NLB 资源，不因本文资源清单删除或改写非目标资源、用户命名或未获授权的 `Outputs`。

## 资源关系

- `ALIYUN::NLB::Listener` 通过 `Properties.LoadBalancerId` 与 `Properties.ServerGroupId` 关联同一模板中已声明的负载均衡和服务器组，不引用外部已有实例 ID。
- `ALIYUN::NLB::LoadBalancer` 与 `ALIYUN::NLB::ServerGroup` 使用同一个 `VpcId`；`ServerGroupType="Instance"` 时，后端服务器必须与服务器组位于同一 VPC。
- NLB 没有 ROS `Rule` 资源；一个监听通过 `ServerGroupId` 关联一个服务器组，不支持路径/域名分流或多目标加权转发，不要套用 ALB 转发规则。
- 后端服务器绑定由 `ALIYUN::NLB::ServerGroup` 的 `Properties.Servers` 表达；该字段可选，离线模板通常不声明，避免写入真实实例 ID。

## 负载均衡 `ALIYUN::NLB::LoadBalancer`

| 属性路径 | 类型 / 约束 | 使用要点 |
| --- | --- | --- |
| `Properties.VpcId` | String，必填 | 引用已声明 VPC 参数，不写真实 ID。 |
| `Properties.AddressType` | `Intranet` / `Internet`，必填 | 私网取 `Intranet`，且不要添加 `AllocationId`、`EipType` 等公网地址字段。 |
| `Properties.AddressIpVersion` | `Ipv4` / `DualStack`，可选 | IPv4 规范写法为 `Ipv4`，不是 ALB 常见的 `IPv4`；按需声明。 |
| `Properties.ZoneMappings` | 列表，必填，至少 2 项 | 每项必填 `ZoneId`、`VSwitchId`；完整路径为 `Properties.ZoneMappings[].ZoneId` / `Properties.ZoneMappings[].VSwitchId`；每个可用区只绑定一个交换机。 |
| `Properties.LoadBalancerBillingConfig.PayType` | String | 配置对象仅有 `PayType`；按量付费取 `PostPay`，其他取值以目标 schema 为准。 |
| `Properties.DeletionProtectionConfig.Enabled` | String | 取 `"true"` / `"false"`；关闭保护写字符串 `"false"`，不是布尔值。 |
| `Properties.DeletionProtectionConfig.Reason` | String，可选 | 按需写入。 |
| `Properties.LoadBalancerName` | String，2~128 字符 | 以字母开头，可含字母、数字、点、下划线、连字符。 |

- `Fn::GetAtt` 可读取 `LoadBalancerId`、`DNSName`、`AddressType`、`AddressIpVersion`、`VpcId`、`ZoneMappings`、`LoadBalancerType`、`Arn`；域名只能用 `DNSName`。

## 服务器组 `ALIYUN::NLB::ServerGroup`

| 属性路径 | 类型 / 约束 | 使用要点 |
| --- | --- | --- |
| `Properties.VpcId` | String，必填 | 与关联 `LoadBalancer` 的 `VpcId` 一致。 |
| `Properties.ServerGroupName` | String，必填 | 遵循用户命名。 |
| `Properties.ServerGroupType` | String | `Instance` 表示实例类型后端；不把该场景推广为唯一取值。 |
| `Properties.Protocol` | `TCP` / `UDP` / `TCPSSL` | 与监听协议和使用场景匹配。 |
| `Properties.AddressIPVersion` | 可选 | 属性名与 `LoadBalancer` 的 `AddressIpVersion` 大小写不同；如需声明，IPv4 取 `Ipv4`。未声明默认 IPv4，不要仅为完整性添加。 |
| `Properties.Scheduler` | 可选 | 例如 `Wrr`；用户要求时写入。 |
| `Properties.Servers` | 可选列表 | 用户要求绑定后端时才声明。 |

用户要求健康检查时，在 `ALIYUN::NLB::ServerGroup` 上写 `Properties.HealthCheckConfig`；该配置不属于 `Listener`。只写入用户要求、schema 必填或条件必填字段：

| 字段完整路径 | 类型 / 范围 | 条件 |
| --- | --- | --- |
| `Properties.HealthCheckConfig.HealthCheckEnabled` | 布尔值 | 取 `true` / `false`，不要写成字符串。 |
| `Properties.HealthCheckConfig.HealthCheckType` | `Tcp` / `Http` | 区分大小写，决定 HTTP 专属字段是否生效。 |
| `Properties.HealthCheckConfig.HealthCheckConnectPort` | 整数 0~65535 | `0` 表示复用后端端口。 |
| `Properties.HealthCheckConfig.HealthCheckUrl` | 以 `/` 开头，1~80 字符 | 仅 `HealthCheckType="Http"` 生效。 |
| `Properties.HealthCheckConfig.HttpCheckMethod` | `GET` / `HEAD` | 仅 HTTP 健康检查生效。 |
| `Properties.HealthCheckConfig.HealthCheckHttpCode` | 列表，例如 `["http_2xx"]` | 仅 HTTP 健康检查生效。 |
| `Properties.HealthCheckConfig.HealthCheckDomain` | String | 仅 HTTP 健康检查生效。 |
| `Properties.HealthCheckConfig.HealthCheckInterval` | 整数 5~50 | 按需设置。 |
| `Properties.HealthCheckConfig.HealthyThreshold` | 整数 2~10 | 按需设置。 |
| `Properties.HealthCheckConfig.UnhealthyThreshold` | 整数 2~10 | 按需设置。 |
| `Properties.HealthCheckConfig.HealthCheckConnectTimeout` | 整数 1~300 | 按需设置。 |

- `Tcp` 健康检查不要额外生成 HTTP 专属字段。
- `Fn::GetAtt` 可读取 `ServerGroupId`。

## 监听 `ALIYUN::NLB::Listener`

| 属性路径 | 类型 / 约束 | 使用要点 |
| --- | --- | --- |
| `Properties.LoadBalancerId` | String，必填 | 用 `Ref` 指向同一模板内用户指定的 `LoadBalancer` 逻辑 ID。 |
| `Properties.ServerGroupId` | String，必填 | 用 `Ref` 指向同一模板内用户指定的 `ServerGroup` 逻辑 ID。 |
| `Properties.ListenerProtocol` | `TCP` / `UDP` / `TCPSSL`，必填 | 协议决定证书等条件字段。 |
| `Properties.ListenerPort` | 整数 1~65535，必填 | 单端口监听使用。 |
| `Properties.CertificateIds` | 服务器证书 ID 列表 | `TCPSSL` 必填；由参数传入，不写死真实 ID。 |
| `Properties.CaEnabled` / `Properties.CaCertificateIds` | 可选 | 双向认证；用户要求启用时配置。 |
| `Properties.SecurityPolicyId` | 可选 | 仅对 `TCPSSL` 生效。 |
| `Properties.StartPort` / `Properties.EndPort` | 端口段字段 | 端口段监听；与单一 `ListenerPort` 场景互斥。 |
| `Properties.IdleTimeout` / `Properties.ProxyProtocolEnabled` / `Properties.ListenerDescription` | 可选 | 用户要求或条件需要时写入。 |

- `Fn::GetAtt` 可读取 `ListenerId`、`ListenerPort`。

## 条件与易错差异

- `LoadBalancer` 用 `AddressIpVersion`；`ServerGroup` 用 `AddressIPVersion`；IPv4 取值均为 `Ipv4`。
- `DeletionProtectionConfig.Enabled` 是字符串；`HealthCheckEnabled` 是布尔值。
- 健康检查配置属于 `ALIYUN::NLB::ServerGroup`，不是 `Listener`。
- `TCPSSL` 必须提供 `CertificateIds`；`TCP` / `UDP` 不需要服务器证书。
- `ListenerProtocol` 只有 `TCP` / `UDP` / `TCPSSL`，没有 HTTP/HTTPS 监听。
- 端口段监听不能与单一 `ListenerPort` 按单端口语义同时使用。
- 私网 `Intranet` 不添加 `AllocationId`、`EipType` 等公网地址字段。

## 引用、参数与输出

- `LoadBalancerId`、`ServerGroupId`、`ListenerId` 输出可写 `{"Ref":"<对应资源逻辑ID>"}` 或 `{"Fn::GetAtt":["<对应资源逻辑ID>","<同名属性>"]}`。
- `DNSName` 只能通过 `Fn::GetAtt` 从 `LoadBalancer` 读取：

```json
{"DNSName": {"Fn::GetAtt": ["<load-balancer-logical-id>", "DNSName"]}}
```

- `Ref` 核对已声明资源、已声明参数或支持的 ROS 伪参数；`Fn::GetAtt` 单独核对已声明资源及其合法属性。
- 网络、可用区、交换机、证书等真实标识输入，使用无 `Default` 的必填 `String` 参数，除非用户明确要求其他形式。
- 修改既有模板时，未获授权修改的 `Outputs` 表达式必须逐字保留。

## 本地校验与修复重试

| 症状 | 处理 |
| --- | --- |
| JSON 解析失败 | 检查编码、括号、逗号、引号、数字与布尔类型；修正后重跑本地解析。 |
| 未知字段或层级不匹配 | 查完整属性路径，确认字段属于 `Properties`、配置对象还是列表项，再最小替换。 |
| 枚举或大小写错误 | 核对 `Ipv4`、`PostPay`、`"true"` / `"false"`、`Tcp` / `Http` 等写法；只改错误值，不改用户命名。 |
| 引用不成立 | 分别核对 `Ref` 与 `Fn::GetAtt` 的目标声明和合法属性；修复引用闭环。 |
| 条件字段缺失或多余 | 按协议、健康检查类型、私网/公网、单端口/端口段判断；补条件必填项，删除未要求可选项。 |
| 出现真实云资源 ID | 改为已声明参数或既有引用。 |

- 必要校验：JSON 为 UTF-8 对象；`ROSTemplateFormatVersion` 与用户任务一致且不加未允许顶层结构；监听、负载均衡、服务器组引用闭环正确；相关资源 VPC 一致；无外部资源 ID；用户指定逻辑 ID、名称、端口、路径、输出表达式和未授权改动逐字保留。
- 检查字段路径、类型、枚举、范围和条件；修复后重跑受影响检查。模板未变化不重复提交；新错误先修复，再判断旧问题是否解决。
- 不为消除错误添加未要求可选字段、修改用户命名或重写无关资源；用户契约与产品字段冲突时，保留用户命名和未授权改动并报告冲突，字段层级、类型、枚举、范围以目标官方 ROS schema 为准。
- 离线校验通过只说明语法与结构符合本次要求，不宣称库存、权限、部署、运行时健康或宿主验收已验证。

## 未覆盖能力与官方来源

本文未展开公网或双栈地址、IPv6、端口段监听细节、双向认证、自定义安全策略、后端服务器绑定、跨可用区与连接优雅中断等高级开关。需要这些能力时，以目标 ROS schema 和用户要求为准，不能凭通用负载均衡经验生成无法离线确认的约束。URL 仅记录来源，离线任务不需要联网访问：

- [ALIYUN::NLB::LoadBalancer](https://www.alibabacloud.com/help/en/ros/developer-reference/aliyun-nlb-loadbalancer)
- [ALIYUN::NLB::ServerGroup](https://www.alibabacloud.com/help/en/ros/developer-reference/aliyun-nlb-servergroup)
- [ALIYUN::NLB::Listener](https://www.alibabacloud.com/help/en/ros/developer-reference/aliyun-nlb-listener)
