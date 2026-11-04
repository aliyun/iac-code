# ALB（应用型负载均衡）ROS 产品参考

本文面向阿里云 ROS 原生模板中的 `ALIYUN::ALB::*` 资源，整理常用的资源关系、字段约束、嵌套路径和必要校验。本文是常用能力摘要，不是完整 schema；未覆盖字段、枚举、默认值、输出属性、地域或版本差异，应查询目标版本的官方 ROS schema/文档。除下文列出的已核对字段外，不要凭字段名、CLI、API 或 SDK 参数形状推断 ROS 层级。模板生成和离线校验不证明库存、配额、证书权限、网络关系、部署或运行时健康已经验证。

下文路径中的 `Properties` 均指 ROS 资源声明中的顶层 `Properties`，不是资源内部名为 `Properties` 的字段。

## 快速导航

| 需求 | 先查位置 | 关键点 |
|---|---|---|
| 创建或修改 ALB 实例 | `ALIYUN::ALB::LoadBalancer` 章节 | VPC、地址、版本、可用区映射、计费 |
| 创建或修改后端服务器组 | `ALIYUN::ALB::ServerGroup` 章节 | 组类型、后端协议、健康检查、VPC 关系 |
| 创建或修改前端监听 | `ALIYUN::ALB::Listener` 章节 | 协议/端口、HTTPS 证书、默认转发 |
| 创建或修改转发规则 | `ALIYUN::ALB::Rule` 章节 | 条件、优先级、方向、动作顺序和权重 |
| 确认跨资源关系 | “资源关系” | LoadBalancer、ServerGroup、Listener、Rule 的关联路径 |
| 定位字段完整路径 | 对应资源章节 | 先按资源归属确定 `Properties` 下的完整路径，再查目标 schema |
| 输出资源标识或属性 | “引用与输出” | `Ref` 与 `Fn::GetAtt` 的合法目标不同 |
| 选择核对工具 | “字段查找顺序与工具选择” | 已覆盖字段用本文；未覆盖或版本差异用官方 schema；结构问题用静态校验 |
| 处理校验失败 | “失败定位与重试”与“必要校验” | 分类错误、最小修复、只复核受影响路径 |

## 通用规则

- 用户明确指定的资源逻辑 ID 必须逐字保留；`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。
- `Ref` 可以引用模板中已声明的 `Resources` 条目、已声明的 `Parameters`，或当前 ROS 模板支持的伪参数；返回值含义由被引用对象决定。
- `Fn::GetAtt` 只能获取已声明资源的合法输出属性，不能把参数或伪参数作为被取属性的资源。两者合法目标不同，不要合并成同一组规则。
- 资源 ID 输出使用 `{"Ref": "<resource-logical-id>"}`；资源属性输出使用 `{"Fn::GetAtt": ["<resource-logical-id>", "<attribute-name>"]}`，不要伪造常量。
- VPC、可用区、交换机、证书等外部 ID 应参数化，不写入真实云资源 ID。模板只能声明这些输入的引用关系；真实可用区库存、网络关系、证书权限和配额需部署前另行核验。
- 生成或修改业务模板时，只补充用户要求、必填或条件必填的属性，不主动补齐可选默认值。本文介绍可选能力时，也应说明其启用条件。
- 只修改用户当前涉及或明确授权的资源；不要因为本文包含某个资源章节而删除既有非目标资源，也不要用 `ALIYUN::SLB` / CLB 资源替代 ALB。
- ALB 不使用 CLB/SLB 的 `MasterZoneId`、`SlaveZoneId`、`LoadBalancerSpec`、`VServerGroupId`、`BackendServerPort` 等字段；不要跨产品套用字段结构。
- 本文示例值只说明对应场景；不要把地址类型、实例版本、协议、可用区数量、端口、路径、权重或目标数量等单次选择推广为 ALB 的全局限制或默认值。
- 模板编写、字段核对和结构校验优先离线完成。离线校验通过不等于云资源可创建或已健康运行。

## 字段查找顺序与工具选择

1. 先读取用户需求、现有模板和错误信息，确认涉及的具体 `ALIYUN::ALB::*` 资源、字段路径和用户命名边界；不要先扩展到无关资源。
2. 已由本文覆盖的常用字段、枚举、范围、嵌套路径和输出属性，先查“快速导航”定位的资源章节；涉及输出、跨资源关系或字段归属时，同时核对“资源关系”与“引用与输出”。不要凭 API、SDK、CLI 参数形状或字段名相似性推断 ROS 层级。
3. 常见容易混淆的字段归属按目标资源和完整路径定位：健康检查在 `ServerGroup` 的 `Resources.<logical-id>.Properties.HealthCheckConfig`；证书和 `Http2Enabled` 在 `Listener` 的 `Resources.<logical-id>.Properties.Certificates` / `Http2Enabled`；粘性会话在 `ServerGroup` 的 `Resources.<logical-id>.Properties.StickySessionConfig`；默认转发在 `Listener` 的 `Resources.<logical-id>.Properties.DefaultActions`；`Direction` 在 `Rule` 的 `Resources.<logical-id>.Properties.Direction`；规则动作和权重在 `Rule` 的 `Resources.<logical-id>.Properties.RuleActions`。
4. 本文未覆盖的字段、默认值、版本差异、地域差异、输出属性或校验报错，应查询目标版本的官方 ROS schema/文档。优先使用环境中已提供的官方资料或 schema；查询范围应围绕该资源与完整属性路径，不要为单点疑问广泛检索或通读无关资料。
5. 语法、资源类型、必填字段、嵌套路径和引用目标使用本次环境允许的静态解析/模板校验工具检查。静态校验用于发现模板结构问题，不是未知字段的 schema 来源；若校验器与官方 schema 冲突，以目标版本 schema 为准并记录差异。
6. 生成或离线校验任务不要调用云 API、查询库存、部署或预览；只有用户明确授权且环境允许时才执行在线操作，并不得把离线生成结果说成运行时已验证。

## 失败定位与重试

- 保留错误原文、资源逻辑 ID、完整属性路径和使用的模板版本；先把错误分为语法/结构、字段路径或类型、枚举/大小写、数值范围、条件必填、引用目标、用户命名保留等类别。不要把不同类别混在一次随机修改中。
- 按报错指向的具体资源和完整属性路径核对本文或官方结构，再做最小修复。一次只验证一个明确假设，避免随机补字段、套用 CLB 结构、改变协议假设、替换逻辑 ID 或添加无关资源。
- 修改后先复核被修改路径及其依赖的 `Ref` / `Fn::GetAtt` 目标，再运行覆盖该资源和引用关系的静态检查；如果修改了顶层结构、跨资源引用或输出，则运行整体模板检查。没有必要重复已经通过且不受本次修改影响的检查。
- 若校验器提示字段或结构不受支持，先核对拼写、大小写、嵌套层级、资源类型和目标 schema 版本，再考虑修改；不要直接换用相似 API/SDK 字段名。
- 若官方资料与模板行为冲突、字段无法确认或权限不足，应明确记录未知并询问用户或查询目标 schema，不要用猜测、默认值或无关示例代替确认。
- 终端错误、证据缺失、来源变更或未解决校验错误不得忽略；不要通过重试掩盖原因，也不要宣称未经执行的能力已经通过。

## 资源关系

- `ALIYUN::ALB::LoadBalancer` 在指定 `VpcId` 中创建 ALB；`ZoneMappings` 将可用区映射到交换机。`Ref` 返回实例 ID；已核对的常用属性为 `LoadBalancerId`、`DNSName`，其中 `DNSName` 属于 LoadBalancer，不属于 Listener。
- `ALIYUN::ALB::ServerGroup` 是后端转发目标。`Instance` 或 `Ip` 类型应配置 `VpcId`，并与 ALB 位于同一 VPC；`Fc` 类型的字段要求不要从 `Instance` / `Ip` 规则推断，应查询对应 schema。
- `ALIYUN::ALB::Listener` 通过 `LoadBalancerId` 关联已声明的 LoadBalancer；默认转发或规则转发通过 `ServerGroupId` 关联已声明的 ServerGroup。`Ref` 返回监听 ID；已核对输出为 `ListenerId`。
- `ALIYUN::ALB::Rule` 通过 `ListenerId` 关联已声明的 Listener。已核对输出为 `RuleId`；其他输出或版本差异以目标官方 attributes schema 为准。
- 服务器成员绑定不是 ServerGroup 的顶层字段，由单独的后端绑定资源处理（例如 `ALIYUN::ALB::BackendServerAttachment`，其字段以该资源官方文档为准）。是否声明空组或绑定成员由用户需求决定，不要为了补齐默认值而添加成员。
- 除非用户要求，不要额外创建 VPC、交换机、ECS、EIP、带宽包或后端绑定资源。

## LoadBalancer 实例资源

`ALIYUN::ALB::LoadBalancer` 的必填 `Resources.<logical-id>.Properties` 字段：

- `VpcId: String`
- `AddressType: String`：枚举 `Internet`、`Intranet`，首字母大写；不要套用 CLB 的小写 `internet` / `intranet`。
- `LoadBalancerEdition: String`：支持 `Basic`、`Standard`、`StandardWithWaf`。
- `ZoneMappings: List`：每个列表项必填 `ZoneId` 和 `VSwitchId`。通常使用至少两个不同可用区、每区一个交换机；少数本地地域存在单可用区例外，因此不要把“必须两个可用区”写成所有地域的全局强制规则。
- `LoadBalancerBillingConfig: Map`：其中 `PayType` 必填；按量付费使用 `PostPay`。

常用可选字段和条件：

- `AddressIpVersion: String`：接受 `IPv4`、`Ipv4`、`DualStack`。`IPv4` 是规范写法；不要声称 `Ipv4` 别名无效。
- `AddressAllocatedMode: String`：支持 `Fixed`、`Dynamic`。不要把某个版本或场景的默认值推广到所有版本；需要依赖默认值时查询目标版本 schema。
- `DeletionProtectionEnabled: Boolean`：默认 `false`。仅在用户显式要求或条件要求时设置。

当用户要求私网 ALB 且未要求公网能力时，`AddressType: Intranet` 不应附加 `AllocationId`、公网 EIP 或带宽包字段。健康检查、监听、转发规则和成员绑定不属于 LoadBalancer 顶层字段。

## ServerGroup 服务器组资源

`ALIYUN::ALB::ServerGroup` 的必填字段：

- `Resources.<logical-id>.Properties.ServerGroupName: String`
- `Resources.<logical-id>.Properties.HealthCheckConfig: Map`
- `Resources.<logical-id>.Properties.HealthCheckConfig.HealthCheckEnabled: Boolean`

常用字段与条件：

- `ServerGroupType` 支持 `Instance`、`Ip`、`Fc`；`Fc` 的后端协议仅支持 `HTTP`。
- `Protocol` 支持 `HTTP`、`HTTPS`、`gRPC`、`GRPC`。后端协议可对应的前端监听协议为：

| 后端 `Protocol` | 可对应前端协议 |
|---|---|
| `HTTP` | `HTTP`、`HTTPS`、`QUIC` |
| `HTTPS` | `HTTPS` |
| `gRPC` / `GRPC` | `HTTPS`、`QUIC` |

- `Instance` 或 `Ip` 类型应设置 `VpcId`，并保持与 ALB 同 VPC。模板只能声明该关系，实际网络连通性和库存需部署前另行核验。
- `Resources.<logical-id>.Properties.HealthCheckConfig` 常用字段：
  - `HealthCheckProtocol`：支持 `HTTP`、`HTTPS`、`TCP`、`gRPC`、`GRPC`。
  - `HealthCheckPath: String`：必须以 `/` 开头。
  - `HealthCheckConnectPort: Integer`：范围 `0..65535`；`0` 表示使用后端端口。
  - `HealthCheckCodes`：HTTP 健康检查使用 `http_2xx` 等类别组成的列表；gRPC 使用其专用状态码字符串或区间。不要把 HTTP 类别规则推广到所有协议。
  - `HealthCheckInterval: Integer`：范围 `1..50`。
  - `HealthCheckTimeout: Integer`：范围 `1..300`。
  - `HealthyThreshold: Integer`、`UnhealthyThreshold: Integer`：范围均为 `2..10`。
- `HealthCheckInterval` 和 `HealthCheckTimeout` 必须使用完整字段名；`Interval`、`Timeout` 不是 `HealthCheckConfig` 的字段别名。
- `Scheduler` 支持 `Wrr`、`Wlc`、`Sch`、`Uch`。
- 粘性会话配置是独立 Map，启用开关的完整路径为 `Resources.<logical-id>.Properties.StickySessionConfig.StickySessionEnabled: Boolean`；不要把 `StickySessionEnabled` 提升为资源 `Properties` 的直接字段。未获用户或条件要求时，不要主动添加可选健康检查、调度或粘性会话配置。

## Listener 监听资源

`ALIYUN::ALB::Listener` 的必填字段：

- `Resources.<logical-id>.Properties.LoadBalancerId: String`：应通过 `Ref` 引用已声明的 LoadBalancer。
- `ListenerProtocol: String`：前端支持 `HTTP`、`HTTPS`、`QUIC`，不是 TCP/UDP。
- `ListenerPort: Integer`：范围 `1..65535`。
- `DefaultActions: List`

条件约束：

- HTTPS 监听需要 `Properties.Certificates: List`，列表项的 `Properties.Certificates[].CertificateId` 用于引用证书；证书 ID 应参数化。
- `Properties.Http2Enabled: Boolean` 仅在 HTTPS 相关场景使用。
- 健康检查配置位于 ServerGroup 的 `HealthCheckConfig`，不是 Listener 的 CLB `HealthCheck` 字段。
- 当前 ROS Listener 默认动作结构不声明 `Weight`，不要把 Rule 的加权转发结构复制到 `DefaultActions`。

默认转发的完整字段路径是 `Resources.<logical-id>.Properties.DefaultActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId`。最短结构示例：

```json
{
  "Type": "ForwardGroup",
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      { "ServerGroupId": { "Ref": "<server-group-logical-id>" } }
    ]
  }
}
```

## Rule 转发规则资源

`ALIYUN::ALB::Rule` 的必填字段：

- `Resources.<logical-id>.Properties.ListenerId: String`：应通过 `Ref` 引用已声明的 Listener。
- `RuleName: String`
- `Priority: Integer`：范围 `1..10000`，同一监听内唯一；数值越小优先级越高。
- `RuleConditions: List`
- `RuleActions: List`

结构约束：

- `Direction: String` 是资源 `Properties` 下的字段，取值 `Request` 或 `Response`，与 `RuleActions` 同层；不属于 `RuleActions` 列表项。请求路由使用 `Request`。
- 路径条件位于 `RuleConditions` 列表项，字段为 `Type: Path` 和 `PathConfig.Values`（路径字符串列表）。
- 转发动作位于 `RuleActions` 列表项，常用路径为：
  - `Properties.RuleActions[].Type`
  - `Properties.RuleActions[].Order`
  - `Properties.RuleActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId`
  - `Properties.RuleActions[].ForwardGroupConfig.ServerGroupTuples[].Weight`
- `Order: Integer` 范围为 `1..50000`。转发到单个服务器组时 `Weight` 可缺省，缺省值为 `100`；转发到多个服务器组时，每个组必须显式提供各自整数 `Weight`，范围 `0..100`。
- Rule 的 `Order` / `Weight` 结构与 Listener 默认转发不同，不要互相套用。
- 多个目标服务器组是否允许重复，应以目标官方 schema/API 约束为准；不要把某个受控场景的额外限制写成 ALB 通用限制。

路径条件的最短结构示例：

```json
{
  "Type": "Path",
  "PathConfig": {
    "Values": ["<path-string>"]
  }
}
```

## 引用与输出

- 用户明确指定的资源逻辑 ID、参数名和未授权修改的内容必须保留。
- `Ref` 的合法目标包括已声明资源、已声明参数和支持的 ROS 伪参数；`Fn::GetAtt` 的被取属性对象只能是已声明资源，且属性名必须是该资源类型支持的输出属性。
- `ALIYUN::ALB::LoadBalancer`：`Ref` 返回实例 ID；`Fn::GetAtt` 可使用已核对的 `LoadBalancerId`、`DNSName`。
- `ALIYUN::ALB::ServerGroup`：`Ref` 返回服务器组 ID；已核对输出为 `ServerGroupId`。
- `ALIYUN::ALB::Listener`：`Ref` 返回监听 ID；已核对输出为 `ListenerId`。
- `ALIYUN::ALB::Rule`：`Ref` 返回规则 ID；已核对输出为 `RuleId`。
- 输出不要写成固定字符串；`DNSName` 是 LoadBalancer 的属性，不是 Listener 的属性。若目标 ROS 版本的输出列表不同，应以该版本官方 attributes schema 为准。

## 必要校验

编写或修改模板时，先按“字段查找顺序与工具选择”确定字段来源和工具，再执行本次任务允许的静态检查；出现错误时按“失败定位与重试”处理。至少检查：

- JSON/YAML 语法可解析，ROS 顶层结构和资源声明位置正确。
- `ZoneMappings`、`ListenerPort`、健康检查端口/间隔/超时/阈值、Rule 的 `Priority` / `Order` / `Weight` 等范围正确。
- HTTPS 证书、私网地址模式、ServerGroup VPC、多服务器组权重等条件规则符合用户请求。
- Listener 默认转发、Rule 加权转发、健康检查和粘性会话分别位于正确的资源和属性路径。
- `Ref` / `Fn::GetAtt` 目标存在且类型合法；资源依赖通过引用表达式建立，不能靠名称相似或固定字符串伪造。
- 仅包含用户要求或必填/条件必填的属性，未主动补齐可选默认值或无关资源。
- 离线校验通过后，仍不宣称库存、配额、权限、计费、部署或运行时健康已验证。

## 官方来源

- [ALIYUN::ALB::LoadBalancer](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer)
- [ALIYUN::ALB::ServerGroup](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup)
- [ALIYUN::ALB::Listener](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener)
- [ALIYUN::ALB::Rule](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule)
