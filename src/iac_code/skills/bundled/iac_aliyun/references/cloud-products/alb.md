# ALB 应用型负载均衡

## 通用约束

- 用户明确指定的资源逻辑 ID 应逐字保留，Ref / Fn::GetAtt 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。禁止固化测试题 ID，但当前用户的命名要求必须遵守。
- VpcId、ZoneId、VSwitchId、CertificateId 等外部标识必须参数化，不得写入真实云资源 ID。
- DNSName 是 LoadBalancer 的属性，不是 Listener 属性；输出时必须使用 Fn::GetAtt 获取，不得伪造常量字符串。
- 私网场景不应附加 AllocationId、公网 EIP 或带宽包。
- 不要使用 ALIYUN::SLB 的 LoadBalancerSpec、MasterZoneId、SlaveZoneId 等字段。
- 本资料是核对过的常用字段摘要，不是完整 schema。未覆盖属性、能力变化或校验错误仍应查询目标版本的官方 schema/文档（如 GetResourceType），不得全局禁止检索。

## ALIYUN::ALB::LoadBalancer

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| VpcId | String | ALB 所在 VPC |
| AddressType | String | Intranet 或 Internet（首字母大写），不能套用 CLB 的小写 internet/intranet |
| LoadBalancerEdition | String | Basic、Standard、StandardWithWaf |
| ZoneMappings | List | 每项包括必填 ZoneId 和 VSwitchId；通常至少两个不同可用区，每区一个交换机；少数本地地域有单 AZ 例外，不得声称所有地域都无例外要求两区 |
| LoadBalancerBillingConfig | Map | 嵌套 PayType:String，按量付费为 PostPay |

### 可选属性

- AddressIpVersion:String：接受 IPv4、Ipv4 和 DualStack，IPv4 是规范写法，不能声称 Ipv4 别名无效。
- AddressAllocatedMode:String：Fixed 或 Dynamic。旧版描述与升级版的 IP 模式行为应按目标版本核验。
- DeletionProtectionEnabled:Boolean：默认 false。

### 输出属性

- LoadBalancerId：Ref 返回实例 ID。
- DNSName：Fn::GetAtt 获取域名。

## ALIYUN::ALB::ServerGroup

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| ServerGroupName | String | 服务器组名称 |
| HealthCheckConfig | Map | 健康检查配置，其中 HealthCheckEnabled:Boolean 必填 |

### 协议与健康检查

- ServerGroupType 为 Instance、Ip 或 Fc；Fc 的后端协议仅 HTTP。
- Protocol 支持 HTTP、HTTPS、gRPC、GRPC；gRPC/GRPC 都是当前实现接受的写法。
- 协议匹配关系：HTTP 后端可对应 HTTP/HTTPS/QUIC 前端；HTTPS 后端对应 HTTPS 前端；gRPC 后端对应 HTTPS/QUIC 前端。
- Instance 或 Ip 类型服务器组应配置 VpcId，并与 ALB 同 VPC。

### HealthCheckConfig 子字段

| 子字段 | 类型 | 说明 |
|--------|------|------|
| HealthCheckEnabled | Boolean | 必填 |
| HealthCheckProtocol | String | HTTP/HTTPS/TCP/gRPC/GRPC |
| HealthCheckPath | String | HTTP 场景以 / 开头 |
| HealthCheckConnectPort | Integer | 0..65535，默认 0 表示后端端口；必须使用完整名称，HealthCheckPort 不是此处的字段别名 |
| HealthCheckCodes | List | HTTP 场景为 http_2xx 等类别列表，不能将 HTTP 类别规则推广至所有协议；gRPC 使用其专用状态码字符串或区间 |
| HealthCheckInterval | Integer | 1..50 |
| HealthCheckTimeout | Integer | 1..300 |
| HealthyThreshold | Integer | 2..10 |
| UnhealthyThreshold | Integer | 2..10 |

### 其他配置

- Scheduler 支持 Wrr、Wlc、Sch、Uch。
- StickySessionConfig 与健康检查是不同的嵌套配置，不可混淆。
- 成员绑定由单独的 BackendServerAttachment 负责，不使用 CLB VServerGroupId 或 BackendServerPort 顶层字段。

### 输出属性

- ServerGroupId：Ref 返回组 ID。

## ALIYUN::ALB::Listener

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| LoadBalancerId | String | 所属 ALB 实例 ID |
| ListenerProtocol | String | HTTP、HTTPS、QUIC（不支持 TCP/UDP） |
| ListenerPort | Integer | 1..65535 |
| DefaultActions | List | 默认转发结构 |

### DefaultActions 结构

DefaultActions 项 Type=ForwardGroup，ForwardGroupConfig.ServerGroupTuples 中每项 ServerGroupId 指向服务器组。当前 ROS Listener 的这个默认动作结构不声明 Weight，不要把 Rule 的加权结构误放进去。

### HTTPS 特有配置

- Certificates:List，其项用 CertificateId 引用证书。
- Http2Enabled:Boolean，仅 HTTPS 相关场景使用。

### 注意事项

- 健康检查配置在 ServerGroup 上，而不是 Listener 的 CLB HealthCheck 字段。

### 输出属性

- ListenerId：Ref 返回监听 ID。

## ALIYUN::ALB::Rule

### 必填属性

| 属性 | 类型 | 说明 |
|------|------|------|
| ListenerId | String | 所属监听 ID |
| RuleName | String | 规则名称 |
| Priority | Integer | 1..10000，同一监听内唯一，越小优先级越高 |
| RuleConditions | List | 匹配条件 |
| RuleActions | List | 转发动作 |

### RuleConditions 结构

路径条件：RuleConditions 项 Type=Path，PathConfig.Values 为路径字符串列表。

### RuleActions 结构

转发动作：RuleActions 项 Type=ForwardGroup、Order:Integer（1..50000），ForwardGroupConfig.ServerGroupTuples 中每项 ServerGroupId 指向组。单组 Weight 缺省为 100；多组必须显式提供各自整数 Weight（0..100）。Rule 的多个转发组权重与默认监听转发字段不同。

### 方向

Direction 可为 Request 或 Response；请求路由使用 Request。

### 输出属性

- RuleId：Ref 返回规则 ID。其他输出以目标版本的 attributes schema 为准。

## 可用性查询

部署前查询 ALB 库存时，调用 DescribeZones API 获取指定地域下支持 ALB 的可用区列表，再与模板 Parameters 中的 ZoneId 取交集。

## 来源

- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule
- 本地复核：iac-code 的 ros_resource_value_specs.json / ros_official_resource_index.json；ROS 源码 ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py

<notes>
本次从空白 bootstrap seed（仅 `# ALB` 标题）创建完整的中文 ALB 产品 reference。根据 MISSION.md "CREATE NEW REFERENCE" 指令，KEEP_BASE 不是有效完成状态，必须基于离线可信资料生成实质性内容。

变更章节与依据：
1. **通用约束**：写入用户指定逻辑 ID 逐字保留规则（MISSION.md 明确要求）、外部标识参数化、DNSName 归属、私网 EIP 禁令、SLB 字段禁用等；依据 product-context.md "引用、范围和证据"节及 LoadBalancer 节。
2. **LoadBalancer 节**：覆盖必填属性表、AddressType 大小写约束、LoadBalancerBillingConfig 嵌套结构、AddressIpVersion 别名有效性、ZoneMappings 双区例外声明；来源 product-context.md LoadBalancer 段。base1 轨迹中 solver 使用了 `LoadBalancerProtocol` 和顶层 `PayType`，与 product-context 描述的 `AddressIpVersion` 和 `LoadBalancerBillingConfig.PayType` 存在差异，reference 以官方资料为准。
3. **ServerGroup 节**：HealthCheckConfig 子字段表格化呈现完整名称及数值范围（HealthCheckConnectPort 而非 HealthCheckPort），协议匹配关系、StickySessionConfig 独立性、成员绑定由 BackendServerAttachment 负责；来源 product-context.md ServerGroup 段。base1 template.json 使用了 HealthCheckPort:80，证明字段名易混淆，需显式列出正确名称。
4. **Listener 节**：DefaultActions 结构不声明 Weight（与 Rule 区分），HTTPS 特有 Certificates/Http2Enabled，健康检查不在 Listener 上；来源 product-context.md Listener 段。
5. **Rule 节**：Priority 唯一性、Path 条件结构、Order 范围、单组 Weight 缺省 vs 多组显式、Direction 取值；来源 product-context.md Rule 段。
6. **可用性查询**：提供 ALB 库存查询 API 调用模式，与 iac-aliyun skill 部署流程对接。
7. **来源标注**：四个官方文档 URL 及本地 ROS 源码复核路径，确保可追溯。

语言处理：所有标题和自然语言正文使用中文；资源类型名、属性名、参数值、代码示例和 URL 保持英文原文。未复制任何任务答案、固定 ID 或特定场景模板片段。适用条件：ROS 原生模板中涉及 ALB 四类核心资源的生成、解释和完善场景。
</notes>
