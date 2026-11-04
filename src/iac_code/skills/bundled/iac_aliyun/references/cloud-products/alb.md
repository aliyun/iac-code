# ALB（应用型负载均衡）ROS 资源参考

本文档覆盖 ROS 原生资源类型 `ALIYUN::ALB::LoadBalancer`、`ALIYUN::ALB::ServerGroup`、`ALIYUN::ALB::Listener`、`ALIYUN::ALB::Rule` 的必填属性、关键约束与输出属性。内容来源于官方文档及已核对的 ROS 资源实现，不替代目标版本的完整 schema 校验。未覆盖属性仍需通过 `GetResourceType` 或官方文档核验，不得将本文摘要当作完整 schema。

## 通用约束

- 用户明确指定的资源逻辑 ID 必须逐字保留；`Ref` / `Fn::GetAtt` 引用时使用该 ID，不得自行改名、添加后缀或用示例名称替代。
- VpcId、VSwitchId、ZoneId、CertificateId 等外部标识必须参数化，禁止写入真实云资源 ID 或固定字符串。
- 资源依赖通过 `Ref` / `Fn::GetAtt` 建立；`DNSName` 是 `ALIYUN::ALB::LoadBalancer` 的属性，不是 Listener 属性，输出时必须用 `Fn::GetAtt`。
- 模板生成阶段不能宣称库存、配额、证书权限或可部署性已核验；这些事实需在真实部署前另行确认。

## ALIYUN::ALB::LoadBalancer

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| VpcId | String | ALB 所属 VPC |
| AddressType | String | `Intranet` 或 `Internet`（首字母大写），不可套用 CLB 的小写写法 |
| LoadBalancerEdition | String | `Basic`、`Standard`、`StandardWithWaf` |
| ZoneMappings | List | 每项包含必填 `ZoneId` + `VSwitchId`；通常至少两个不同可用区，每区一个交换机。少数本地地域存在单 AZ 例外，不得声称所有地域都必须两区 |
| LoadBalancerBillingConfig | Map | `PayType` 必填，按量付费为 `PostPay` |

### 常用可选属性

- `AddressIpVersion`: String，接受 `IPv4`、`Ipv4`、`DualStack`。`IPv4` 是推荐规范写法，不得声称 `Ipv4` 别名无效。
- `AddressAllocatedMode`: String，支持 `Fixed`、`Dynamic`；私网场景通常选 `Dynamic`。不同版本默认值可能不同，应按目标版本核验。
- `DeletionProtectionEnabled`: Boolean，默认 `false`；如需显式关闭可设为 `false`。
- 私网实例不应附加 `AllocationId`、公网 EIP 或带宽包相关字段。
- 不要使用 `ALIYUN::SLB` 的 `LoadBalancerSpec`、`MasterZoneId`、`SlaveZoneId` 等字段。

### 输出属性

- `LoadBalancerId`：实例 ID，可通过 `Ref` 获取。
- `DNSName`：域名，必须通过 `Fn::GetAtt` 获取。
- `Ref` 返回实例 ID。

来源：[ALIYUN::ALB::LoadBalancer](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer)

## ALIYUN::ALB::ServerGroup

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| ServerGroupName | String | 服务器组名称 |
| HealthCheckConfig | Map | 健康检查配置，其中 `HealthCheckEnabled`: Boolean 必填 |

Instance 或 Ip 类型服务器组应配置 `VpcId`，并与 ALB 同 VPC。

### 关键约束

- `ServerGroupType`: String，取值 `Instance`、`Ip`、`Fc`；`Fc` 类型后端协议仅支持 HTTP。
- `Protocol`: String，支持 `HTTP`、`HTTPS`、`gRPC`、`GRPC`（两种 gRPC 写法均被当前实现接受）。
  - HTTP 后端可对应 HTTP/HTTPS/QUIC 前端；HTTPS 后端对应 HTTPS 前端；gRPC 后端对应 HTTPS/QUIC 前端。
- `Scheduler`: String，支持 `Wrr`、`Wlc`、`Sch`、`Uch`。
- `StickySessionConfig` 与健康检查是不同的嵌套配置，不启用粘性会话时仍需正确区分两者结构。`StickySessionConfig.StickySessionEnabled` (Boolean) 控制开关。
- ServerGroup 不使用 CLB 的 `VServerGroupId` 或 `BackendServerPort` 顶层字段；成员绑定由单独的 `BackendServerAttachment` 资源负责，不在 ServerGroup 内配置。

### HealthCheckConfig 字段

| 字段 | 约束 |
|------|------|
| HealthCheckEnabled | Boolean，必填 |
| HealthCheckProtocol | HTTP / HTTPS / TCP / gRPC / GRPC |
| HealthCheckPath | String，必须以 `/` 开头 |
| HealthCheckConnectPort | Integer 0..65535，默认 0 表示使用后端端口 |
| HealthCheckCodes | HTTP 场景为 `http_2xx` 等类别列表，不得将 HTTP 类别规则推广至其他协议；gRPC 健康检查使用专用状态码字符串或区间 |
| HealthCheckInterval | Integer 1..50 |
| HealthCheckTimeout | Integer 1..300。必须使用完整属性名，`Interval`/`Timeout` 不是此处的字段别名 |
| HealthyThreshold | Integer 2..10 |
| UnhealthyThreshold | Integer 2..10 |

### 输出属性

- `ServerGroupId`：组 ID，`Ref` 返回组 ID。

来源：[ALIYUN::ALB::ServerGroup](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup)

## ALIYUN::ALB::Listener

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| LoadBalancerId | String | 关联 ALB 实例 |
| ListenerProtocol | String | 前端协议支持 `HTTP`、`HTTPS`、`QUIC`，不支持 TCP/UDP |
| ListenerPort | Integer | 范围 1..65535 |
| DefaultActions | List | 默认转发动作列表 |

### HTTPS 专属属性

- `Certificates`: List，每项包含 `CertificateId` 引用证书资源。
- `Http2Enabled`: Boolean，仅在 HTTPS 场景使用。

### 默认转发动作结构

```yaml
DefaultActions:
  - Type: ForwardGroup
    ForwardGroupConfig:
      ServerGroupTuples:
        - ServerGroupId: !Ref MyServerGroup
```

> **重要**：当前 ROS Listener 的 DefaultActions 默认转发结构不声明 `Weight`；不要将 Rule 的加权结构误放入此处。单组转发时无需 Weight，多组时以目标版本 schema 为准。

### 常见误区

- 健康检查配置在 ServerGroup 上，而非 Listener。
- 不存在 CLB 风格的 `HealthCheck` 顶层字段。

### 输出属性

- `ListenerId`：监听 ID，`Ref` 返回监听 ID。

来源：[ALIYUN::ALB::Listener](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener)

## ALIYUN::ALB::Rule

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| ListenerId | String | 关联监听 |
| RuleName | String | 规则名称 |
| Priority | Integer | 范围 1..10000，同一监听内唯一，数值越小优先级越高 |
| RuleConditions | List | 匹配条件列表 |
| RuleActions | List | 转发动作列表 |

### 路径条件结构

```yaml
RuleConditions:
  - Type: Path
    PathConfig:
      Values:
        - "/api/*"
```

### 转发动作结构

```yaml
RuleActions:
  - Type: ForwardGroup
    Order: 1  # Integer 1..50000
    ForwardGroupConfig:
      ServerGroupTuples:
        - ServerGroupId: !Ref ApiGroup
          Weight: 100  # 单组可缺省（默认 100），多组必须显式提供各自整数 Weight（0..100）
```

### 关键区分

- Rule 的 `ForwardGroupConfig.ServerGroupTuples` 支持 `Weight`，与 Listener DefaultActions 不同。
- `Order` 范围 1..50000，表示同一规则内动作执行顺序。
- `Direction` 可为 `Request` 或 `Response`；请求路由使用 `Request`。

### 输出属性

- `RuleId`：规则 ID，`Ref` 返回规则 ID。其他输出属性以目标版本的 attributes schema 为准，不得声称 `RuleIds` 在所有版本可用。

来源：[ALIYUN::ALB::Rule](https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule)

## 资源关系速查

```
LoadBalancer (VpcId, ZoneMappings)
    └── Listener (LoadBalancerId, Certificates for HTTPS)
            ├── DefaultActions → ServerGroup
            └── Rule (ListenerId, Priority, Conditions, Actions)
                    └── Actions → ServerGroup

ServerGroup (VpcId, HealthCheckConfig, Protocol)
```

- Listener 和 Rule 通过 `Ref` 引用 LoadBalancer / ServerGroup 的逻辑 ID。
- DNSName 只能通过 `Fn::GetAtt` 从 LoadBalancer 获取，不可伪造常量。
- 模板中不得创建 VPC、VSwitch、ECS、EIP 或 BackendServerAttachment，除非用户明确要求。

## 导航与工具选择指引

- 生成 ALB 模板前必须先读取本文件，确认资源类型、必填属性和约束；不得凭记忆填写。
- 遇到本文未覆盖的属性或校验错误时，调用 `aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "ALIYUN::ALB::<资源类型>"})` 查询完整 schema；若 API 暂时不可用，再回退到 `aliyun_doc_search(category_id=28850, keywords="ALIYUN::ALB::<资源类型>")`。
- 校验失败时优先对照本文「关键约束」节排查常见误用（AddressType 大小写、Listener Weight、HealthCheckConfig 字段名、Protocol 前后端匹配），修复后重试，避免重复读取相同文档。
- DNSName 输出必须使用 `Fn::GetAtt`，不得写成常量或 Ref；ServerGroupId、ListenerId、RuleId 使用 `Ref` 即可。
- 单目标 Rule 的 Weight 可省略；多目标 Rule 必须显式声明每个 ServerGroupTuple 的 Weight，否则校验或运行时行为不确定。

## 证据与适用范围

本文基于以下官方来源及本地复核整理，是常用字段摘要而非完整 schema：

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule
- 本地复核：iac-code 的 `ros_resource_value_specs.json` / `ros_official_resource_index.json`；ROS 源码 `ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py`。

示例场景采用私网、Standard、HTTP 后端等选择，不代表 ALB 全部能力的限制。未覆盖属性、能力变化或校验错误仍应查询目标版本的官方 schema/文档，不得全局禁止 `GetResourceType` 或文档检索。
