# ALB 应用型负载均衡

本文档覆盖 ROS 原生资源类型 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener`、`ALIYUN::ALB::Rule` 的常用字段、约束与输出属性，用于私网/公网 HTTP/HTTPS/QUIC/gRPC 场景的模板生成与校验。未覆盖的属性或版本差异仍应通过 `GetResourceType` 或官方文档核验；本参考不替代完整 schema。

## 通用约束

- 用户明确指定的资源逻辑 ID 必须逐字保留，`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。禁止固化测试题 ID 不代表可以忽略当前用户的命名要求；只写入这条通用规则，不得将某个场景的具体 ID 写入产品文档。
- `VpcId`、交换机、可用区和证书等外部 ID 应参数化为模板 Parameters，不写入真实云资源 ID。
- 资源依赖通过 `Ref` / `Fn::GetAtt` 建立，属性输出不能写成固定字符串。
- `DNSName` 是 `ALIYUN::ALB::LoadBalancer` 的属性，不是 Listener 属性；输出 DNSName 必须使用 `Fn::GetAtt`，不能伪造常量字符串。
- 私网场景不应附加 `AllocationId`、公网 EIP 或带宽包相关属性。若模板中出现 `BandwidthPackageId` 空字符串，应移除该字段。
- 不要使用 `ALIYUN::SLB` 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId` 等字段。
- 成员绑定由单独的 `ALIYUN::ALB::BackendServerAttachment` 负责，不在 ServerGroup 或 Listener 中配置后端服务器。
- 网络输入参数是否实际属于相同 VPC、可用区是否有 ALB 库存、证书/配额/计费事实，需在真实部署前另外核验；模板生成阶段不能宣称已核验。

## ALIYUN::ALB::LoadBalancer

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `VpcId` | String | ALB 所在 VPC，通常参数化 |
| `AddressType` | String | `Intranet`（私网）或 `Internet`（公网），首字母大写；不能套用 CLB 的小写 `internet`/`intranet` |
| `LoadBalancerEdition` | String | `Basic`、`Standard`、`StandardWithWaf` |
| `ZoneMappings` | List | 每项包含必填 `ZoneId` + `VSwitchId`；通常至少两个不同可用区，每区一个交换机；少数本地地域有单 AZ 例外，不得声称所有地域都无例外要求两区 |
| `LoadBalancerBillingConfig` | Map | 子键 `PayType` 必填，按量付费为 `PostPay` |

### 常用可选属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `AddressIpVersion` | String | 接受 `IPv4`、`Ipv4`、`DualStack`；`IPv4` 是规范写法，不能声称 `Ipv4` 别名无效 |
| `AddressAllocatedMode` | String | `Fixed` 或 `Dynamic`；旧版描述与升级版的 IP 模式行为应按目标版本核验，不得据此推断所有版本的默认值 |
| `DeletionProtectionEnabled` | Boolean | 默认 `false` |
| `LoadBalancerName` | String | 实例名称，建议体现业务用途 |

### 输出属性

- `Ref` → 实例 ID（即 LoadBalancerId）
- `Fn::GetAtt` 可用属性：`LoadBalancerId`、`DNSName`

### 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer

## ALIYUN::ALB::ServerGroup

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `ServerGroupName` | String | 服务器组名称 |
| `HealthCheckConfig` | Map | 健康检查配置，子键 `HealthCheckEnabled`（Boolean）必填 |

### 常用可选属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `VpcId` | String | Instance 或 Ip 类型服务器组应配置，且须与 ALB 同 VPC |
| `ServerGroupType` | String | `Instance`、`Ip`、`Fc`；`Fc` 类型的后端协议仅支持 HTTP |
| `Protocol` | String | `HTTP`、`HTTPS`、`gRPC`、`GRPC`；`gRPC`/`GRPC` 均为当前实现接受的写法 |
| `Scheduler` | String | `Wrr`、`Wlc`、`Sch`、`Uch` |
| `StickySessionConfig` | Map | 粘性会话配置，与健康检查是不同的嵌套结构 |

### 前后端协议匹配

| 后端 Protocol | 允许的 Listener 前端协议 |
|---------------|--------------------------|
| HTTP | HTTP、HTTPS、QUIC |
| HTTPS | HTTPS |
| gRPC / GRPC | HTTPS、QUIC |

### HealthCheckConfig 子属性

| 属性 | 类型 / 范围 | 说明 |
|------|-------------|------|
| `HealthCheckEnabled` | Boolean | 必填 |
| `HealthCheckProtocol` | String | `HTTP`、`HTTPS`、`TCP`、`gRPC`、`GRPC` |
| `HealthCheckPath` | String | 以 `/` 开头，HTTP/HTTPS 场景使用 |
| `HealthCheckConnectPort` | Integer 0..65535 | 默认 0 表示后端端口；**字段名为 `HealthCheckConnectPort`，不是 `HealthCheckPort`** |
| `HealthCheckCodes` | List | HTTP 场景为 `http_2xx` 等类别列表；不能将 HTTP 类别规则推广至所有协议 |
| `HealthCheckInterval` | Integer 1..50 | 必须使用完整名称，`Interval` 不是此处的字段别名 |
| `HealthCheckTimeout` | Integer 1..300 | 必须使用完整名称，`Timeout` 不是此处的字段别名 |
| `HealthyThreshold` | Integer 2..10 | |
| `UnhealthyThreshold` | Integer 2..10 | |

gRPC 健康检查使用其专用状态码字符串或区间。

### 输出属性

- `Ref` → ServerGroupId

### 注意事项

- 不使用 CLB 的 `VServerGroupId` 或 `BackendServerPort` 顶层字段
- 服务器组为空时不配置服务器成员，成员绑定由 `ALIYUN::ALB::BackendServerAttachment` 单独负责

### 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup

## ALIYUN::ALB::Listener

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `LoadBalancerId` | String | 通常 `Ref` 指向 LoadBalancer |
| `ListenerProtocol` | String | `HTTP`、`HTTPS`、`QUIC`；不支持 TCP/UDP |
| `ListenerPort` | Integer | 1..65535 |
| `DefaultActions` | List | 默认转发动作列表 |

### DefaultActions 结构

```json
{
  "Type": "ForwardGroup",
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      { "ServerGroupId": { "Ref": "<服务器组逻辑ID>" } }
    ]
  }
}
```

- 当前 ROS Listener 的默认动作结构不声明 `Weight`；不要把 Rule 的加权结构误放入此处。
- 健康检查配置在 ServerGroup 上，而不是 Listener 的 CLB `HealthCheck` 字段。

### 常用可选属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `Http2Enabled` | Boolean | 仅 HTTPS 场景使用 |
| `Certificates` | List | HTTPS 时需要，项中使用 `CertificateId` 引用证书 |

### 输出属性

- `Ref` → ListenerId

### 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener

## ALIYUN::ALB::Rule

仅在需要基于路径/域名等条件路由时使用；简单默认转发只需 Listener 的 `DefaultActions`，不需要 Rule。

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `ListenerId` | String | 所属监听 |
| `RuleName` | String | 规则名称 |
| `Priority` | Integer | 1..10000，同一监听内唯一，越小优先级越高 |
| `RuleConditions` | List | 匹配条件 |
| `RuleActions` | List | 转发动作 |

### RuleConditions 路径条件结构

```json
{ "Type": "Path", "PathConfig": { "Values": ["/api/*", "/web"] } }
```

### RuleActions 转发结构

```json
{
  "Type": "ForwardGroup",
  "Order": 1,
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      { "ServerGroupId": { "Ref": "<组>" }, "Weight": 100 }
    ]
  }
}
```

- `Order`：Integer 1..50000。
- 单组时 `Weight` 缺省为 100；多组必须显式提供各自整数 `Weight`（0..100）。
- Rule 的多转发组权重结构与 Listener 默认转发字段不同，不可混淆。
- `Direction`：`Request`（请求路由）或 `Response`（响应路由）。

### 输出属性

- `Ref` → RuleId
- `RuleIds` 输出并非在所有版本可用，其他输出以目标版本的 attributes schema 为准。

### 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule

## 引用与范围说明

本文件是核对过的常用字段摘要，不是完整 schema。未覆盖属性、能力变化或校验错误仍应查询目标版本的官方 schema/文档，不得全局禁止 `GetResourceType` 或文档检索。示例场景采用私网、Standard、HTTP 后端等选择，不能写成 ALB 全部能力的限制。

本地复核依据：iac-code `ros_resource_value_specs.json` / `ros_official_resource_index.json`；ROS 源码 `ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py`。
