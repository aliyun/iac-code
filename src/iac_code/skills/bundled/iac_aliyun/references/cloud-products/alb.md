# ALB（应用型负载均衡）ROS 资源参考

本文档为 ALIYUN::ALB 系列 ROS 原生资源的产品参考，覆盖 LoadBalancer、ServerGroup、Listener、Rule 四类资源的字段约束、嵌套结构、资源关系与条件规则。内容基于可信离线产品资料及轨迹验证事实整理，适用于私网/公网 HTTP(S)/QUIC/gRPC 场景的模板生成与校验。技术标识、属性名和资源类型保持原文；自然语言说明使用中文。

## 通用约束

- 用户明确指定的资源逻辑 ID 应逐字保留，`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。
- VpcId、交换机、可用区、证书等外部 ID 必须通过 Parameters 参数化输入，禁止写入真实云资源 ID 或固定字符串。
- 资源依赖通过 `Ref` / `Fn::GetAtt` 建立；`DNSName` 是 `ALIYUN::ALB::LoadBalancer` 的属性（`Fn::GetAtt[Alb, DNSName]`），不是 Listener 属性，不可伪造常量。
- 不要使用 `ALIYUN::SLB` 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId`、`VServerGroupId`、`BackendServerPort` 等字段；ALB 与 CLB 资源类型和字段体系不兼容。
- 本文件仅覆盖常用字段摘要；未覆盖属性或版本差异仍需查询目标版本的官方 schema（如 `GetResourceType`），不得全局禁止文档检索。

## ALIYUN::ALB::LoadBalancer

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| `VpcId` | String | 所属 VPC，需参数化 |
| `AddressType` | String | `Intranet` 或 `Internet`（首字母大写），不可使用小写 `internet`/`intranet` |
| `LoadBalancerEdition` | String | `Basic`、`Standard`、`StandardWithWaf` |
| `ZoneMappings` | List | 每项包含必填 `ZoneId` 和 `VSwitchId`；通常至少两个不同可用区，每区一个交换机 |
| `LoadBalancerBillingConfig` | Map | 必填 `PayType`；按量付费为 `PostPay` |

### 可选属性

- **AddressIpVersion**：接受 `IPv4`、`Ipv4`、`DualStack`。`IPv4` 是推荐规范写法，`Ipv4` 别名同样有效，不可声称其无效。
- **AddressAllocatedMode**：支持 `Fixed`、`Dynamic`。旧版与升级版的 IP 模式行为应按目标版本核验，不可据此推断所有版本的默认值。
- **DeletionProtectionEnabled**：Boolean，默认 false。需要显式声明时直接赋值 true/false。
- **LoadBalancerName**：可选，建议体现业务用途，不包含工具名；未指定时由系统自动生成。

### 私网场景约束

私网（`AddressType: Intranet`）不应附加 `AllocationId`、公网 EIP 或带宽包相关属性。

### ZoneMappings 注意事项

通常要求至少两个不同可用区且每区对应不同交换机。少数本地地域存在单 AZ 例外，因此不得声称"所有地域都无例外地要求两区"。每个 ZoneMapping 项的 ZoneId 必须与其 VSwitchId 正确对应（例如 ZoneId1↔VSwitchId1、ZoneId2↔VSwitchId2），错配会导致创建失败。

### 输出属性

- `Ref` 返回实例 ID（即 `LoadBalancerId`）。
- `Fn::GetAtt` 可获取 `DNSName`、`LoadBalancerId` 等。`DNSName` 仅属于 LoadBalancer，不属于 Listener。

### 陷阱

- `LoadBalancerBillingConfig` 缺失会导致校验失败；不要假设默认计费方式。
- 不要使用 `ALIYUN::SLB` 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId` 等字段。

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer

## ALIYUN::ALB::ServerGroup

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| `ServerGroupName` | String | 服务器组名称 |
| `HealthCheckConfig` | Map | 健康检查配置，`HealthCheckEnabled`:Boolean 必填 |

### 关键属性

- **ServerGroupType**：`Instance`、`Ip` 或 `Fc`。Fc 类型的后端协议仅支持 HTTP。
- **Protocol**：`HTTP`、`HTTPS`、`gRPC`、`GRPC`（gRPC/GRPC 均为当前实现接受的写法）。
- **VpcId**：Instance 或 Ip 类型服务器组应配置 VpcId，且必须与 ALB 同 VPC。
- **Scheduler**：`Wrr`、`Wlc`、`Sch`、`Uch`。
- **StickySessionConfig**：粘性会话配置，与健康检查是不同的嵌套结构，不可混淆。

### 前后端协议兼容关系

| 后端协议 | 可用前端协议 |
|----------|-------------|
| HTTP | HTTP、HTTPS、QUIC |
| HTTPS | HTTPS |
| gRPC | HTTPS、QUIC |

### 健康检查配置（HealthCheckConfig）

- **HealthCheckEnabled**：Boolean，必填，显式开启或关闭。
- **HealthCheckProtocol**：`HTTP`、`HTTPS`、`TCP`、`gRPC`、`GRPC`。
- **HealthCheckPath**：以 `/` 开头的路径字符串，HTTP/HTTPS 场景必填。
- **HealthCheckConnectPort**：Integer，范围 0..65535。默认 0 表示使用后端端口；具体端口值以用户需求为准。**字段名为 `HealthCheckConnectPort`，不是 `HealthCheckPort`**。
- **HealthCheckCodes**：HTTP 场景为 `http_2xx` 等类别列表。此规则仅适用于 HTTP 协议，不可推广至其他协议。
- **HealthCheckInterval**：Integer，范围 1..50。
- **HealthCheckTimeout**：Integer，范围 1..300。
- **HealthyThreshold**：Integer，范围 2..10。
- **UnhealthyThreshold**：Integer，范围 2..10。

注意：`Interval` 和 `Timeout` 不是 HealthCheckConfig 下的字段别名，必须使用完整属性名 `HealthCheckInterval` 和 `HealthCheckTimeout`。

### 成员管理

ServerGroup 本身不包含后端成员；成员绑定由单独的 `BackendServerAttachment` 资源负责。不使用 CLB 的 `VServerGroupId` 或 `BackendServerPort` 顶层字段。空服务器组合法，无需配置 Servers。

### 输出属性

- `Ref` 返回服务器组 ID（`ServerGroupId`）。

### 陷阱

- `HealthCheckPort` 是无效字段名，必须使用 `HealthCheckConnectPort`。
- 不要使用 CLB 的 `VServerGroupId`、`BackendServerPort` 顶层字段。

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup

## ALIYUN::ALB::Listener

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| `LoadBalancerId` | String | 所属 ALB，通过 `Ref` 引用 |
| `ListenerProtocol` | String | `HTTP`、`HTTPS`、`QUIC`（不支持 TCP/UDP） |
| `ListenerPort` | Integer | 1..65535 |
| `DefaultActions` | List | 默认转发动作列表 |

### 默认转发结构

```json
{
  "Type": "ForwardGroup",
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      { "ServerGroupId": { "Ref": "TargetGroup" } }
    ]
  }
}
```

**当前 ROS Listener 的默认动作结构不声明 `Weight`**，不可将 Rule 的加权结构误放入 Listener 的 DefaultActions。

### HTTPS 特有属性

- **Certificates**：证书列表，每项用 `CertificateId` 引用证书。仅 HTTPS 监听需要。
- **Http2Enabled**：Boolean，仅 HTTPS 场景使用。

### 健康检查位置

健康检查配置在 ServerGroup 上，不在 Listener 上。不要使用 CLB 风格的 Listener HealthCheck 字段。

### 输出属性

- `Ref` 返回监听 ID（`ListenerId`）。

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener

## ALIYUN::ALB::Rule

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| `ListenerId` | String | 所属监听，通过 `Ref` 引用 |
| `RuleName` | String | 规则名称 |
| `Priority` | Integer | 1..10000，同一监听内唯一，越小优先级越高 |
| `RuleConditions` | List | 匹配条件列表 |
| `RuleActions` | List | 转发动作列表 |

### 路径条件结构

```json
{
  "Type": "Path",
  "PathConfig": {
    "Values": ["/api/*"]
  }
}
```

### 转发动作结构

```json
{
  "Type": "ForwardGroup",
  "Order": 1,
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      { "ServerGroupId": { "Ref": "ApiGroup" }, "Weight": 100 }
    ]
  }
}
```

- `Order`：Integer 1..50000。
- 单目标组 `Weight` 缺省为 100；多组时必须显式提供各自整数 Weight（0..100）。
- Rule 的加权结构与 Listener 默认转发不同，不可混用。

### 关键字段名

- **条件字段为 `RuleConditions`，不是 `Conditions`**。
- **动作字段为 `RuleActions`，不是 `Actions`**。

### Direction

可为 `Request` 或 `Response`；请求路由使用 `Request`。

### 输出属性

- `Ref` 返回规则 ID（`RuleId`）。其他输出属性以目标版本的 attributes schema 为准，不可宣称 `RuleIds` 在所有版本可用。

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule

## 资源依赖与引用关系

```
LoadBalancer ←(Ref)← Listener.LoadBalancerId
ServerGroup  ←(Ref)← Listener.DefaultActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId
ServerGroup  ←(Ref)← Rule.RuleActions[].ForwardGroupConfig.ServerGroupTuples[].ServerGroupId
Listener     ←(Ref)← Rule.ListenerId
```

- Listener 和 Rule 通过 Ref 引用上游资源，确保声明顺序或 DependsOn 正确。
- DNSName 只能通过 `Fn::GetAtt[LoadBalancerLogicalId, DNSName]` 获取，不可从 Listener 获取或使用常量字符串。

## 导航与工具选择指引

- 生成 ALB 模板前应先阅读本文件，确认资源选型和字段约束；不确定属性时再调用 `GetResourceType` 查询完整 schema。
- 网络输入（VpcId、ZoneId、VSwitchId）一律参数化，不写入真实云资源 ID；库存、配额、证书权限等运行时事实不在模板生成阶段核验。
- DNSName 必须通过 `Fn::GetAtt` 获取，禁止伪造常量字符串。
- 避免重复读取相同文档或反复重试已失败的 API；若首次 `GetResourceType` / 文档搜索暂时失败，可等待后重试一次，仍失败则依据已有离线资料继续，不无限循环。
- 本文件覆盖的字段可直接采信，无需再次查证；未覆盖内容才需额外检索，以减少不必要的工具调用和上下文消耗。

## 适用范围与限制

- 本文件覆盖 ALB 常用字段摘要，不是完整 schema。未覆盖属性、能力变化或校验错误仍应查询目标版本官方 schema（通过 `GetResourceType`）或文档检索。
- 示例选择（私网、Standard、HTTP 后端等）不代表 ALB 全部能力的限制。
- 网络输入参数是否属于相同 VPC、可用区是否有 ALB 库存、证书/配额/计费事实需在真实部署前另外核验，模板生成阶段不可宣称已核验。
- 本地复核依据：iac-code 的 `ros_resource_value_specs.json` / `ros_official_resource_index.json`；ROS 源码 `ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py`。
