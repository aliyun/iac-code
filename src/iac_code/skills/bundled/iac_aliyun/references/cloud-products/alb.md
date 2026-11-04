# ALB 应用型负载均衡

ALB（Application Load Balancer）是阿里云面向 HTTP/HTTPS/QUIC 七层场景的负载均衡产品，对应 ROS 资源类型为 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener`、`ALIYUN::ALB::Rule`。与 CLB（`ALIYUN::SLB::*`）字段体系不同，不得混用。未覆盖属性应通过 `GetResourceType` 或官方文档核验，不得全局禁止查询。

## 通用约束

- 用户明确指定的资源逻辑 ID 应逐字保留，`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。
- VpcId、VSwitchId、ZoneId、CertificateId 等外部标识必须通过模板 Parameters 传入，禁止在模板中写入真实云资源 ID。
- 资源依赖通过 `Ref` / `Fn::GetAtt` 建立；输出属性不能写成固定字符串。DNSName 是 `ALIYUN::ALB::LoadBalancer` 的属性，须通过 `Fn::GetAtt` 获取，不是 Listener 属性。
- 后端服务器成员绑定由独立的 `ALIYUN::ALB::BackendServerAttachment` 负责，不在 ServerGroup 属性中配置；若任务不要求挂载后端，则服务器组保持为空。
- 模板生成不能宣称运行时资源已存在，也不证明库存、配额、证书权限或可部署性。

## ALIYUN::ALB::LoadBalancer

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| VpcId | String | 所在 VPC |
| AddressType | String | `Intranet`（私网）或 `Internet`（公网），首字母大写，不可套用 CLB 的小写 `internet`/`intranet` |
| LoadBalancerEdition | String | `Basic`、`Standard`、`StandardWithWaf` |
| ZoneMappings | List | 每项含必填 `ZoneId` 和 `VSwitchId`；通常至少两个不同可用区，每区一个交换机；少数本地地域有单 AZ 例外，不得声称所有地域都无例外要求两区 |
| LoadBalancerBillingConfig | Map | 内含必填 `PayType`；按量付费为 `PostPay` |

### 可选属性

| 属性 | 类型 | 说明 |
|------|------|------|
| DeletionProtectionEnabled | Boolean | 默认 `false` |
| AddressIpVersion | String | 接受 `IPv4`、`Ipv4`、`DualStack`；`IPv4` 是规范写法，不可声称 `Ipv4` 别名无效 |
| AddressAllocatedMode | String | `Fixed` 或 `Dynamic`；按需显式指定，不要假定默认值 |
| LoadBalancerName | String | 实例名称，体现业务用途 |

### 私网场景注意

私网（`AddressType: Intranet`）实例不应附加 `AllocationId`、公网 EIP 或带宽包。

### 输出属性

- `Ref` 返回实例 ID（等同于 `LoadBalancerId`）
- `DNSName`：必须通过 `Fn::GetAtt` 获取，不可伪造常量

### 禁用字段

不要使用 ALIYUN::SLB 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId` 等字段。

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer

## ALIYUN::ALB::ServerGroup

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| ServerGroupName | String | 服务器组名称 |
| HealthCheckConfig | Map | 健康检查配置，内含必填 `HealthCheckEnabled: Boolean` |

### 关键可选属性

| 属性 | 类型 | 说明 |
|------|------|------|
| ServerGroupType | String | `Instance`、`Ip` 或 `Fc`；`Fc` 后端协议仅 HTTP |
| Protocol | String | `HTTP`、`HTTPS`、`gRPC`、`GRPC`；`gRPC`/`GRPC` 均为当前实现接受的写法 |
| VpcId | String | Instance 或 Ip 类型服务器组应配置，且与 ALB 同 VPC |
| Scheduler | String | `Wrr`、`Wlc`、`Sch`、`Uch` |
| StickySessionConfig | Map | 粘性会话配置，与健康检查是不同的嵌套结构 |

### 健康检查配置（HealthCheckConfig）

| 属性 | 类型 | 约束 |
|------|------|------|
| HealthCheckEnabled | Boolean | **必填** |
| HealthCheckProtocol | String | `HTTP`、`HTTPS`、`TCP`、`gRPC`、`GRPC` |
| HealthCheckPath | String | HTTP 场景使用，必须以 `/` 开头 |
| HealthCheckConnectPort | Integer | 0..65535；默认 0 表示后端端口 |
| HealthCheckCodes | List | HTTP 场景为 `http_2xx` 等类别列表；不可将 HTTP 类别规则推广至所有协议 |
| HealthCheckInterval | Integer | 1..50；必须使用完整属性名，`Interval` 不是此处的字段别名 |
| HealthCheckTimeout | Integer | 1..300；必须使用完整属性名，`Timeout` 不是此处的字段别名 |
| HealthyThreshold | Integer | 2..10 |
| UnhealthyThreshold | Integer | 2..10 |

### 协议匹配规则

- HTTP 后端可对应 HTTP/HTTPS/QUIC 前端
- HTTPS 后端对应 HTTPS 前端
- gRPC 后端对应 HTTPS/QUIC 前端

### 注意事项

- ServerGroup 不使用 CLB 的 `VServerGroupId` 或 `BackendServerPort` 顶层字段
- 主要输出 `ServerGroupId`；`Ref` 返回组 ID

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup

## ALIYUN::ALB::Listener

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| LoadBalancerId | String | 所属 ALB 实例 |
| ListenerProtocol | String | `HTTP`、`HTTPS`、`QUIC`；**不支持 TCP/UDP** |
| ListenerPort | Integer | 1..65535 |
| DefaultActions | List | 默认转发动作列表 |

### HTTPS 专属属性

| 属性 | 类型 | 说明 |
|------|------|------|
| Certificates | List | HTTPS 必填；每项含 `CertificateId` 引用证书 |
| Http2Enabled | Boolean | 仅 HTTPS 场景使用 |

### DefaultActions 结构

默认转发结构为 `Type=ForwardGroup`，`ForwardGroupConfig.ServerGroupTuples` 中每项 `ServerGroupId` 指向服务器组。

**重要**：当前 ROS Listener 的 DefaultActions 结构**不声明 Weight**。不要把 Rule 的加权结构误放入 Listener 默认动作。单目标组时无需权重；多目标组的加权转发应通过 Rule 实现。

### 注意事项

- 健康检查配置在 ServerGroup 上，不是 Listener 的 CLB HealthCheck 字段
- 主要输出 `ListenerId`；`Ref` 返回监听 ID

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener

## ALIYUN::ALB::Rule

### 必填属性

| 属性 | 类型 | 约束 |
|------|------|------|
| ListenerId | String | 所属监听 |
| RuleName | String | 规则名称 |
| Priority | Integer | 1..10000；同一监听内唯一，越小优先级越高 |
| RuleConditions | List | 匹配条件列表 |
| RuleActions | List | 转发动作列表 |

### 路径条件结构

```json
{
  "Type": "Path",
  "PathConfig": {
    "Values": ["/api/*"]
  }
}
```

`RuleConditions` 项 `Type=Path`，`PathConfig.Values` 为路径字符串列表。

### 转发动作结构

```json
{
  "Type": "ForwardGroup",
  "Order": 1,
  "ForwardGroupConfig": {
    "ServerGroupTuples": [
      {
        "ServerGroupId": { "Ref": "TargetGroup" },
        "Weight": 70
      }
    ]
  }
}
```

- `Order`: Integer，1..50000
- 单目标组时 `Weight` 缺省默认为 100
- 多目标组时**必须**显式提供各自整数 `Weight`（0..100）
- Rule 的多个转发组权重与 Listener 默认转发字段不同，不可混淆

### Direction

`Direction` 可为 `Request` 或 `Response`；请求路由使用 `Request`。

### 输出属性

- `RuleId`：`Ref` 返回规则 ID
- 其他输出以目标版本的 attributes schema 为准；不宣称 `RuleIds` 在所有版本可用

来源：https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule

## 常见错误规避

| 错误 | 正确做法 |
|------|----------|
| 使用 CLB 小写 `internet` / `intranet` | ALB 必须用 `Internet` / `Intranet` |
| Listener DefaultActions 中加 Weight | Weight 只在 Rule 的 ServerGroupTuples 中使用 |
| 用 `ALIYUN::SLB::LoadBalancerSpec` 等 CLB 字段 | ALB 使用 `LoadBalancerEdition` |
| 将 HealthCheckInterval 写成 Interval | 必须使用完整字段名 |
| DNSName 写成固定字符串 | 必须 `Fn::GetAtt` |
| IPv4 写成 `ipv4` 小写 | 规范写法为 `IPv4`（`Ipv4` 也接受但不推荐） |

## 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule
- 本地复核：iac-code 的 `ros_resource_value_specs.json` / `ros_official_resource_index.json`；ROS 源码 `ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py`
