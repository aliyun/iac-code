# 阿里云 ALB ROS 产品参考

本参考面向阿里云应用型负载均衡 ALB 的原生 ROS 模板，覆盖 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener` 和 `ALIYUN::ALB::Rule` 的常用能力、字段约束、资源关系和离线校验要点。内容是常用字段与嵌套关系摘要，不是完整 schema；未覆盖属性、版本差异或校验冲突，应查询目标版本官方 ROS schema / 文档，不要从 API / SDK 参数名、CLB / SLB 文档或字段相似性推断 ROS 层级。ALB 与 CLB / SLB 是不同产品族，不要互相套用字段。

## 快速导航

| 处理目标 | 优先查看 |
|---|---|
| 选择资源、建立依赖或确认引用链 | “资源关系与引用”及对应资源章节 |
| 确认必填字段、枚举、范围或嵌套路径 | 对应资源章节；健康检查字段见 ServerGroup 的 `HealthCheckConfig` |
| 区分默认转发与条件转发 | “监听器”与“转发规则”章节 |
| 生成 Outputs 或检查 `Ref` / `Fn::GetAtt` | “输出与引用” |
| 决定查找顺序、本地校验或排错重试 | “适用范围与查找顺序” |
| 交付前复查模板 | “交付前必要检查” |

## 适用范围与查找顺序

1. 先确定用户要求、既有模板结构和目标 ROS 属性完整路径，例如 `Properties.LoadBalancerBillingConfig.PayType`；区分 `Properties`、嵌套配置对象和列表项，不要凭字段名或 API / SDK 参数名猜测层级。
2. 查找顺序为“本文已覆盖且适用条件匹配的字段 → 目标版本 ROS schema → 官方资源文档”。未覆盖属性、版本差异、疑似冲突或校验报错，应带着具体资源、完整路径和错误信息查询；无法确认时明确说明待确认，不要用运行时行为、CLB / SLB 字段或非官方示例替代 schema。
3. 生成或修改模板优先本地读写。交付前先用 JSON / YAML 解析器确认语法，再检查目标资源的必填字段、枚举 / 范围、引用目标和依赖关系；修正后只复查受影响的属性、引用和依赖。离线证据不足且任务、权限和网络限制允许时，才做有边界的官方 schema / 文档查询；任务明确禁止网络或云 API 时保持离线并报告待确认，不要为通过校验调用云 API、查询库存、预览、询价或部署。
4. 排错输入优先使用结构化错误、最小相关模板片段或最小变更差异；仅当需要定位具体失败、未获证据支持的规则或冲突时，才查阅原始证据，不要默认通读全部历史记录。
5. 每次重试前先定位一个具体原因并完成一次针对性修改。解析器给出行号、路径或引用名时，先修正该目标；同一错误没有新证据时不要重复查询或随机尝试层级 / 字段名，也不要盲目追加可选字段。仍无法确认时，记录待确认项和已完成的离线检查。

## 通用编写规则

- 用户明确指定的资源逻辑 ID 必须逐字保留。`Ref` 和 `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加前后缀或用示例名称替代。
- 生成或修改业务模板时，仅补充用户要求、必填或条件必填的属性，不主动补齐可选默认值；本文可以介绍常用可选能力及启用条件，是否写入模板由用户需求决定。
- VPC、交换机、可用区、证书等外部 ID 应参数化或引用既有资源，不写固定云资源 ID。资源间依赖用 `Ref` 或 `Fn::GetAtt` 表达，属性输出不能伪造常量。
- `Ref` 可以引用已声明资源、已声明参数或 ROS 支持的伪参数。校验引用时先确认目标类别，不要把所有 `Ref` 都限定为资源引用，也不要引用未声明的参数或资源。
- 本文的资源类型约束只适用于用户要求涉及的 ALB 资源；不得据此删除用户模板中既有的非目标资源，也不得在用户未要求时为 ALB 依赖创建 VPC、交换机、ECS、EIP、带宽包或后端绑定资源。
- 本文的示例选择和条件约束不能推广为 ALB 全部能力或所有地域的全局限制。模板语法与字段离线校验通过，不证明库存、权限、配额、证书可用性、计费事实、部署或运行时健康已验证。

## 资源关系与引用

```text
ALIYUN::ALB::LoadBalancer
  └── ALIYUN::ALB::Listener    Properties.LoadBalancerId
        └── ALIYUN::ALB::Rule  Properties.ListenerId

ALIYUN::ALB::ServerGroup       与 ALB 所在 VPC 一致
  ← Listener DefaultActions / Rule RuleActions
```

- `ALIYUN::ALB::Listener` 通过 `Properties.LoadBalancerId` 指向 LoadBalancer；`ALIYUN::ALB::Rule` 通过 `Properties.ListenerId` 指向 Listener。
- Listener 默认转发与 Rule 条件转发都通过 `ForwardGroupConfig.ServerGroupTuples` 的列表项指向 ServerGroup；二者的动作层级和字段结构不同，不能混用。
- `Instance` 或 `Ip` 类型 ServerGroup 应配置 `Properties.VpcId`，并与 ALB 所在 VPC 一致。
- 服务器成员绑定不是 ServerGroup 的顶层字段，由单独的 `BackendServerAttachment` 资源负责；本文未覆盖其字段，创建或修改前应查询目标官方 schema，且仅当用户明确要求后端成员时才处理。
- `Ref` 返回资源 ID：LoadBalancer 为 `LoadBalancerId`，ServerGroup 为 `ServerGroupId`，Listener 为 `ListenerId`，Rule 的已核对输出为 `RuleId`。`DNSName` 是 LoadBalancer 的属性，应使用 `Fn::GetAtt` 获取，不是 Listener 属性，也不能写成常量。

## 负载均衡器（ALIYUN::ALB::LoadBalancer）

必填顶层 `Properties`：

- `VpcId: String`
- `AddressType: String`
- `LoadBalancerEdition: String`
- `ZoneMappings: List`
- `LoadBalancerBillingConfig: Map`

字段与条件约束：

- `Properties.AddressType` 只接受 `Intranet` 或 `Internet`，首字母大写；不要套用 CLB 的小写 `intranet` / `internet`。
- `Properties.LoadBalancerEdition` 支持 `Basic`、`Standard`、`StandardWithWaf`。
- `Properties.ZoneMappings` 每个列表项必填 `ZoneId` 和 `VSwitchId`。常见配置为不同可用区各配一个交换机；少数本地地域存在单可用区例外，不要把“必须两个可用区”写成全局规则。用户指定映射数量或对应关系时，应逐项保留。
- `Properties.LoadBalancerBillingConfig.PayType` 必填；按量付费使用 `PostPay`。
- `Properties.AddressIpVersion` 接受 `IPv4`、`Ipv4` 和 `DualStack`；`IPv4` 是常用规范写法，不能声称 `Ipv4` 别名无效。
- `Properties.AddressAllocatedMode` 支持 `Fixed` 和 `Dynamic`；是否显式设置及默认值应按用户要求和目标版本核验。
- `Properties.DeletionProtectionEnabled: Boolean` 默认 `false`；仅在用户要求或部署策略需要时显式设置。
- `AddressType: Intranet` 私网场景不应附加 `AllocationId`、公网 EIP 或带宽包；`AddressType: Internet` 涉及地址计费、EIP 或带宽字段时，先按目标版本官方 schema 确认。
- 不要在 ALB LoadBalancer 中使用 CLB 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId` 等字段。
- 常用输出为 `LoadBalancerId` 和 `DNSName`；`Ref` 返回实例 ID。

## 服务器组（ALIYUN::ALB::ServerGroup）

必填字段：

- `Properties.ServerGroupName: String`；用户指定名称时逐字保留。
- `Properties.HealthCheckConfig: Map`。
- `Properties.HealthCheckConfig.HealthCheckEnabled: Boolean`；健康检查配置属于 ServerGroup，不是 Listener。

字段与条件约束：

- `Properties.ServerGroupType` 支持 `Instance`、`Ip`、`Fc`。`Instance` 或 `Ip` 类型应配置 `Properties.VpcId` 并与 ALB 同 VPC；`Fc` 的后端协议仅支持 `HTTP`。
- `Properties.Protocol` 支持 `HTTP`、`HTTPS`、`gRPC`、`GRPC`；`gRPC` 与 `GRPC` 都是当前实现接受的写法。协议匹配关系：HTTP 后端可对应 HTTP / HTTPS / QUIC 前端；HTTPS 后端对应 HTTPS 前端；gRPC 后端对应 HTTPS / QUIC 前端。
- `Properties.Scheduler` 支持 `Wrr`、`Wlc`、`Sch`、`Uch`；仅在用户要求或需要表达调度语义时设置。
- `Properties.StickySessionConfig` 是独立嵌套 Map，启用开关的完整路径为 `Properties.StickySessionConfig.StickySessionEnabled: Boolean`；不要提升为 ServerGroup 顶层属性。
- ServerGroup 不使用 CLB 的 `VServerGroupId`、`BackendServerPort` 等顶层字段，也不内联声明服务器成员。
- 常用输出为 `ServerGroupId`；`Ref` 返回组 ID。

`Properties.HealthCheckConfig` 下的常用字段：

- `HealthCheckProtocol`：接受 `HTTP`、`HTTPS`、`TCP`、`gRPC`、`GRPC`；按后端协议和用户要求选择。
- `HealthCheckPath`：HTTP 健康检查路径，必须以 `/` 开头。
- `HealthCheckConnectPort: Integer`：范围 `0..65535`；默认 `0` 表示使用后端端口。
- `HealthCheckCodes`：HTTP 健康检查状态码类别列表，例如 `http_2xx`；该类别规则不能推广到非 HTTP 协议，gRPC 健康检查使用专用状态码字符串或区间。
- `HealthCheckInterval: Integer` 范围 `1..50`；`HealthCheckTimeout: Integer` 范围 `1..300`；`HealthyThreshold` 和 `UnhealthyThreshold` 范围 `2..10`。必须使用完整字段名，`Interval`、`Timeout` 不是该 Map 内这些字段的别名。

## 监听器（ALIYUN::ALB::Listener）

必填 `Properties`：

- `LoadBalancerId: String`，通常引用本模板 LoadBalancer。
- `ListenerProtocol: String`。
- `ListenerPort: Integer`，范围 `1..65535`。
- `DefaultActions: List`。

字段与条件约束：

- 前端协议支持 `HTTP`、`HTTPS`、`QUIC`，不是 TCP / UDP。
- HTTPS 监听必须配置 `Properties.Certificates` 列表；每个列表项使用 `CertificateId` 引用证书。证书等外部 ID 应参数化，不写固定云资源 ID。
- `Properties.Http2Enabled: Boolean` 仅在 HTTPS 相关场景使用。
- 默认转发结构为 `Properties.DefaultActions` 列表项 `Type: ForwardGroup`，通过 `ForwardGroupConfig.ServerGroupTuples` 的列表项 `ServerGroupId` 指向 ServerGroup。当前 ROS Listener 的默认转发结构不声明 `Weight`，不要把 Rule 的加权转发字段复制到 `DefaultActions`。
- 健康检查字段在 ServerGroup 上，不要给 Listener 添加 CLB 风格的 `HealthCheck` 配置。
- 常用输出为 `ListenerId`；`Ref` 返回监听 ID。

最小结构示意（占位符，不是完整模板答案）：

```yaml
DefaultActions:
  - Type: ForwardGroup
    ForwardGroupConfig:
      ServerGroupTuples:
        - ServerGroupId:
            Ref: <用户指定的服务器组逻辑ID>
```

## 转发规则（ALIYUN::ALB::Rule）

仅在用户明确要求路径、域名等转发规则时创建。必填 `Properties`：

- `ListenerId: String`，通常引用本模板 Listener。
- `RuleName: String`。
- `Priority: Integer`，范围 `1..10000`，同一监听内唯一；数值越小优先级越高。
- `RuleConditions: List`。
- `RuleActions: List`。

字段与条件约束：

- 路径条件使用 `Properties.RuleConditions` 列表项 `Type: Path`，并在 `PathConfig.Values` 中提供路径字符串列表；路径格式应按用户要求和目标 schema 校验。
- 转发动作使用 `Properties.RuleActions` 列表项 `Type: ForwardGroup`；`Order: Integer` 范围 `1..50000`，并通过 `ForwardGroupConfig.ServerGroupTuples[*].ServerGroupId` 指向 ServerGroup。
- Rule 的 `ForwardGroupConfig.ServerGroupTuples` 中，单个目标组可缺省 `Weight`，缺省值为 `100`；多个目标组必须为每个列表项显式提供整数 `Weight`，范围 `0..100`。该加权结构只适用于 `RuleActions`，与 Listener 默认转发不同，不要混用。
- `Properties.Direction: String` 支持 `Request` 或 `Response`，与 `RuleActions` 同层，不属于 `RuleActions` 列表项。请求路由使用 `Request`。
- 受控场景中的“目标组不重复”是场景要求，不能写成未获来源支持的通用 API 约束。
- 当前核对的输出包含 `RuleId`；不要宣称 `RuleIds` 在所有版本可用，其他输出以目标版本 attributes schema 为准。

## 输出与引用

- 引用资源：`{"Ref": "<用户指定逻辑ID>"}`。
- 获取属性：`{"Fn::GetAtt": ["<用户指定逻辑ID>", "<属性名>"]}`。
- `DNSName` 是 LoadBalancer 属性，必须通过 `Fn::GetAtt` 从 LoadBalancer 获取，不是 Listener 属性。
- ServerGroup、Listener、Rule 的 `Ref` 分别返回 `ServerGroupId`、`ListenerId`、`RuleId`。
- 不要将属性输出写成固定字符串或伪造常量。

## 交付前必要检查

1. JSON / YAML 语法可解析；资源类型与用户授权范围和既有模板范围一致，不因 ALB 知识删除或改写非目标资源。
2. 每个 ALB 资源的必填 `Properties`、嵌套 Map / List 路径、枚举和数值范围完整且正确。
3. 用户指定的逻辑 ID、名称、协议、端口、路径和其他业务约束逐字或按数值语义保留；未授权字段未被修改。
4. `Ref` / `Fn::GetAtt` 均使用用户指定的逻辑 ID，且目标为已声明资源、已声明参数或支持的 ROS 伪参数；外部 ID 已参数化。
5. 检查 LoadBalancer → Listener → Rule 引用链、Instance / Ip 类型 ServerGroup 与 ALB 的 VPC 一致性，以及 HTTPS 条件字段与前端协议的匹配。
6. 健康检查字段位于 ServerGroup；默认转发与规则转发结构分别正确；输出为对应的 `Ref` 或 `Fn::GetAtt` 表达式，不含固定云资源 ID、示例 DNS 常量或用户未要求的数据。
7. 对未知字段、版本差异或校验冲突记录“需查询目标官方 schema”，不要用猜测补齐；仅做离线检查时不宣称库存、权限、配额、证书、计费、部署或运行时健康已验证。

## 官方来源

- [ALIYUN::ALB::LoadBalancer](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer)
- [ALIYUN::ALB::ServerGroup](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup)
- [ALIYUN::ALB::Listener](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener)
- [ALIYUN::ALB::Rule](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule)
