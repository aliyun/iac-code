# 经典型 CLB（SLB）ROS 模板参考

本文面向经典型 CLB（SLB）的 ROS 模板生成与已有模板局部修改，覆盖私网 IPv4 实例、TCP/UDP/HTTP/HTTPS 监听、默认后端与虚拟服务器组、HTTPS 服务端证书、协议适配健康检查和输出引用。本文是常用字段摘要，不是完整产品 schema；未覆盖字段、版本差异或校验报错应查询目标资源与目标版本的官方 ROS schema。模板离线校验只证明结构、字段取值和引用正确，不证明库存、配额、权限、云部署、健康检查运行状态或真实流量可用性。

## 快速导航与资源选择

先按交付目标定位相关资源和字段，再按固定顺序定型并填写字段；未确认产品、后端模式和协议前，不要检索或复制完整 schema：

1. 确认产品仍是经典型 CLB（SLB），不要把任务改写为 ALB 或 NLB。
2. 区分生成新模板与局部修改已有模板；修改任务先读取实际资源类型、逻辑 ID 和对象路径。
3. 私网 IPv4 显式使用 `ALIYUN::SLB::LoadBalancer.Properties.AddressType="intranet"`、`AddressIPVersion="ipv4"`。
4. 确定后端模式：默认服务器组用 `ALIYUN::SLB::BackendServerAttachment`，虚拟服务器组用 `ALIYUN::SLB::VServerGroup`。
5. 确定 `ALIYUN::SLB::Listener.Properties.Protocol`，同步适配证书、健康检查、会话保持和四层/七层字段。
6. 最后核对实例、监听、后端资源之间的引用和用户要求的输出属性。

| 场景 | 资源组合 | 关键字段 |
|---|---|---|
| 默认服务器组 | `ALIYUN::SLB::LoadBalancer` + `ALIYUN::SLB::Listener` + `ALIYUN::SLB::BackendServerAttachment` | 监听用 `Listener.Properties.BackendServerPort`；附件资源挂载已有后端。 |
| 虚拟服务器组 | `ALIYUN::SLB::LoadBalancer` + `ALIYUN::SLB::Listener` + `ALIYUN::SLB::VServerGroup` | 监听用 `Listener.Properties.VServerGroupId`；组内每个后端项自带 `Port` 和 `Weight`。 |
| HTTPS | `ALIYUN::SLB::LoadBalancer` + `ALIYUN::SLB::Listener` + 所需后端资源 | `Protocol="https"` 时必须提供 `Listener.Properties.ServerCertificateId`。 |
| TCP/UDP | `ALIYUN::SLB::LoadBalancer` + `ALIYUN::SLB::Listener` + 用户要求的后端资源 | 分别适配 `HealthCheckType="tcp"` 或 UDP `Req`/`Exp`，不要照搬七层字段。 |
| 局部修改 | 只使用用户授权的对象路径 | 先定位资源逻辑 ID、`Properties` 与 Map/数组父属性，再做最小标量修改。 |

协议与后端模式可按用户要求组合，但字段组合必须完整适配；不要把一次场景组合推广为全局限制，也不要主动补齐未要求的可选默认值。

协议速查：证书字段在 `ALIYUN::SLB::Listener.Properties` 下；健康检查键在 `ALIYUN::SLB::Listener.Properties.HealthCheck` 下；会话保持键在 `ALIYUN::SLB::Listener.Properties.Persistence` 下。

| `Protocol` | 证书字段 | 常用健康检查形态 | 会话保持要点 |
|---|---|---|---|
| `tcp` | 不使用 `ServerCertificateId` | `HealthCheckType="tcp"` | `PersistenceTimeout` |
| `udp` | 不使用 `ServerCertificateId` | `Req`/`Exp` | `PersistenceTimeout` |
| `http` | 不使用 `ServerCertificateId` | `HealthCheckType="http"`，按需 `Switch`、`URI`、`HttpCode`、`HealthCheckMethod` | `StickySession` 及 Cookie 字段 |
| `https` | 必填 `ServerCertificateId` | 同 `http` | `StickySession` 及 Cookie 字段，可选 `EnableHttp2`、`TLSCipherPolicy` |

## 适用资源与总体关系

涉及本产品时使用以下资源，不要把经典型 CLB 场景改写为 ALB 或 NLB，也不要混用 ALB/NLB 的属性或返回值：

- `ALIYUN::SLB::LoadBalancer`：创建负载均衡实例。
- `ALIYUN::SLB::Listener`：在实例上创建 `tcp`、`udp`、`http` 或 `https` 监听。
- `ALIYUN::SLB::BackendServerAttachment`：把已有后端挂载到监听默认服务器组。
- `ALIYUN::SLB::VServerGroup`：创建虚拟服务器组，并由监听通过 `VServerGroupId` 选择。

核心关系：

- `ALIYUN::SLB::Listener.Properties.LoadBalancerId`：`Ref` 引用本模板负载均衡实例。
- `ALIYUN::SLB::VServerGroup.Properties.LoadBalancerId`：`Ref` 引用本模板负载均衡实例。
- `ALIYUN::SLB::Listener.Properties.VServerGroupId`：`Fn::GetAtt` 获取虚拟服务器组的 `VServerGroupId`。
- `ALIYUN::SLB::BackendServerAttachment.Properties.LoadBalancerId`：`Ref` 引用本模板负载均衡实例。
- 外部已有资源 ID（VPC、交换机、后端 ECS/ENI/ECI、HTTPS 证书等）应定义为无 `Default` 的输入参数并用 `Ref` 引用；不要硬编码、猜测或为了通过校验补默认值。
- 默认服务器组与虚拟服务器组是两种后端选择方式：监听使用 `VServerGroupId` 时，不要再用 `BackendServerPort` 表达同一后端选择；`BackendServerAttachment` 也不能替代监听中的 `VServerGroupId`。

最短关系示例（占位符不是建议的逻辑 ID）：

```json
"Listener": {
  "Type": "ALIYUN::SLB::Listener",
  "Properties": {
    "LoadBalancerId": {"Ref": "<loadBalancerLogicalId>"},
    "VServerGroupId": {"Fn::GetAtt": ["<vServerGroupLogicalId>", "VServerGroupId"]}
  }
}
```

## 引用、命名与参数规则

- 用户明确指定的资源逻辑 ID 必须逐字保留；`Ref` 与 `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例占位符替代。
- `Ref` 可以引用已声明资源、已声明参数或 ROS 支持的伪参数。
- `Fn::GetAtt` 只能作用于已声明资源及其官方支持的属性，不能把参数或伪参数当作被取属性的资源。
- 不要因字段名相似而合并两种运算符的合法目标；输出表达式应与用户要求的精确形式一致，不得因 `Ref` 也返回实例 ID 而擅自替换指定的 `Fn::GetAtt`。
- 产品资源类型限制只适用于本次涉及的 CLB 资源，不能据此删除已有模板中的非目标资源。
- 用户指定的参数名、`Type`、是否必填、是否允许 `Default` 和取值约束必须逐项满足；只参数化确实需要外部输入的资源，不为示例补齐无关参数。

## 生成与局部修改规则

生成业务模板时：

- JSON 模板根固定使用 `"ROSTemplateFormatVersion": "2015-09-01"`。
- 用户指定的参数名、参数 `Type`、无 `Default`、资源数量、资源逻辑 ID、资源类型和输出名必须逐项满足；未授权的 `Mappings`、`Transform`、`Rules` 或额外资源不要添加。
- 只补充用户要求、必填或条件必填的属性，不主动补齐可选默认值；本文列出可选能力是为了识别语义和启用条件，不代表每次都要写。
- `HealthCheck`、`Persistence` 是 `ALIYUN::SLB::Listener.Properties` 下的 Map 属性，内部键不能平铺到 `Properties` 顶层；Map 的键集合应按用户给定内容精确表达。
- `BackendServers`、`BackendServerList`、`BackendServerWeightList` 等数组属性应在父属性处整体书写或断言，保持给定顺序和元素内容，不要用数组下标路径、排序、去重或拆散字段来表达同一约束。
- 切换协议或后端模式时，同步核对 `HealthCheck`、`BackendServerPort`/`VServerGroupId`、证书字段和后端资源的字段组合；不要只改 `Protocol` 枚举后保留不适用的配置。
- 字符串型开关（如 `Gzip`、`EnableHttp2`、`StickySession`、`XForwardedFor`）必须保持字符串字面量，不要改写为 Boolean；`DeletionProtection`、`StartListener` 等 Boolean 字段不要写成字符串。

局部修改已有模板时：

- 先读取原始模板并定位实际对象路径：`Resources` → 资源逻辑 ID → `Type` → `Properties` → Map 或数组父属性。
- 若任务明确限定为局部修改，只修改用户指定且对象路径中已存在的资源标量；保留资源、逻辑 ID、参数、无关属性、Map 键集合、数组顺序、输出和模板元数据。
- 不把列表或对象整体重写为绕过授权路径的方式；数组中未明确要求修改的元素必须原样保留。
- 若任务提供 `preserve_template`、`allowed_changes` 或新值断言，必须与其约束一致；确认修改前后唯一差异来自授权路径。
- 修改 `Protocol` 或后端模式属于结构性变更，只有用户明确授权时才执行，并同步核对相关条件字段。

## 实例：ALIYUN::SLB::LoadBalancer

常用 `Properties` 字段：

- `AddressType`：`internet` 或 `intranet`，小写枚举；私网实例显式使用 `intranet`。
- `AddressIPVersion`：`ipv4` 或 `ipv6`；经典私网 IPv4 场景显式使用 `ipv4`。
- `VpcId`、`VSwitchId`：指定 VPC 型实例网络位置，通常引用无 `Default` 的参数。
- `LoadBalancerSpec`：性能保障型规格，取 `slb.s1.small`、`slb.s2.small`、`slb.s2.medium`、`slb.s3.small`、`slb.s3.medium`、`slb.s3.large`、`slb.s3.xlarge`、`slb.s3.xxlarge`。
- `InstanceChargeType`：`PayBySpec` 或 `PayByCLCU`。
- `InternetChargeType`：仅公网实例使用 `paybybandwidth` 或 `paybytraffic`。
- `DeletionProtection`：Boolean。
- `ModificationProtectionStatus`：`NonProtection` 或 `ConsoleProtection`。
- 常用可选字段：`LoadBalancerName`、`Bandwidth`、`MasterZoneId`、`SlaveZoneId`、`ResourceGroupId`、`Tags`。
- 当前资源语法不含 `PayType`、`Duration`、`PricingCycle` 输入字段；`PayType` 只出现在返回属性中，不要从 ECS 计费字段推断。

`Ref` 返回负载均衡实例 ID。

## 监听：ALIYUN::SLB::Listener

基础字段（`Protocol`、`ListenerPort`、`Bandwidth`、`LoadBalancerId` 必填）：

- `Protocol`：小写枚举 `http`、`https`、`tcp`、`udp`。
- `ListenerPort`：1~65535。
- `Bandwidth`：`-1` 或 1~1000。
- `LoadBalancerId`：`Ref` 引用本模板负载均衡实例，或引用无 `Default` 的已有实例参数。
- `BackendServerPort`：默认服务器组模式的后端端口。
- `VServerGroupId`：虚拟服务器组模式，取值为 `{"Fn::GetAtt": ["<vServerGroupLogicalId>", "VServerGroupId"]}`。

协议相关字段：

- `https` 必须提供 `ServerCertificateId`；已有服务端证书应参数化。可选 `CACertificateId`、`EnableHttp2`（字符串 `on`/`off`）、`TLSCipherPolicy`。
- `http`/`https` 可用 `Gzip`（字符串 `"true"`/`"false"`）、`IdleTimeout`（1~60）、`RequestTimeout`（1~180）、`HttpConfig`。`HttpConfig` 的内部字段未在本文覆盖时，必须查询官方 schema，不要按 API/SDK 参数名推断。
- `Scheduler`：`wrr` 或 `wlc`。
- `StartListener`：Boolean。

### 健康检查：Listener.Properties.HealthCheck

`HealthCheck` 是 Map 属性，不是顶层平铺字段。常用键：

- `Switch`：`on`/`off`；仅 HTTP/HTTPS 生效。
- `HealthCheckType`：`tcp` 或 `http`。
- `Domain`：`$_ip`、自定义域名或空。
- `URI`：以 `/` 开头。
- `HttpCode`：`http_2xx`、`http_3xx`、`http_4xx`、`http_5xx`，可逗号组合。
- `HealthCheckMethod`：`head` 或 `get`，仅 HTTP/HTTPS 且 `Switch="on"`。
- `Port`：0~65535；`0` 表示使用后端端口。
- `Interval`：1~5。
- `Timeout`：1~50；小于 `Interval` 时会被忽略。
- `HealthyThreshold`、`UnhealthyThreshold`：1~10。
- `Req`、`Exp`：UDP 健康检查请求/响应串，仅字母和数字，最长 64。

协议适配：HTTP/HTTPS 使用 `HealthCheckType="http"` 并按需配置 `Switch`、`URI`、`HttpCode`、`HealthCheckMethod`；TCP 使用 `HealthCheckType="tcp"`；UDP 使用 `Req`/`Exp`。不要把七层专属字段照搬到四层监听。

### 会话保持：Listener.Properties.Persistence

`Persistence` 是 Map 属性。常用键：

- HTTP/HTTPS：`StickySession`（`on`/`off`）、`StickySessionType`（`insert`/`server`）、`CookieTimeout`（1~86400，`StickySessionType="insert"` 时必填）、`Cookie`（`StickySessionType="server"` 时必填，1~200 字符，不能以 `$` 开头，只含字母数字，不含逗号、分号或空格）。
- HTTP/HTTPS 转发头：`XForwardedFor`、`XForwardedFor_proto`、`XForwardedFor_SLBID`、`XForwardedFor_SLBIP`、`XForwardedFor_ClientSrcPort`、`XForwardedFor_SLBPORT`，均为 `on`/`off`。
- TCP/UDP：`PersistenceTimeout`，0~1000。

## 默认后端：ALIYUN::SLB::BackendServerAttachment

`Properties.LoadBalancerId` 必填。生成新模板时按用户要求选择后端表达方式；用户只要求一种后端模式时不要额外补另一种，用户明确要求同时保留默认后端与虚拟服务器组时则分别建立真实引用：

- `BackendServerList`：实例引用或参数引用组成的列表；可搭配同顺序的 `BackendServerWeightList`，未提供时权重默认 100。
- `BackendServers`：对象列表，每项 `ServerId` 必填，`Weight` 必填且取 0~100（官方摘要标注默认 100），可选 `Type`（`ecs`、`eni`、`eci`）、`ServerIp`、`Description`（1~80 字符）。

官方摘要同时说明：若二者同时出现，`BackendServers` 生效而 `BackendServerList` 被忽略。不要在新模板中依赖该优先级；修改已有模板时，只有用户明确要求才改变原有表达方式。单次最多 20 台后端。用户给出后端列表时，应按原顺序在父属性处整体表达，不主动补默认项。

## 虚拟服务器组：ALIYUN::SLB::VServerGroup

必填 `Properties.LoadBalancerId`、`Properties.VServerGroupName`。`Properties.BackendServers` 为对象列表，每项：

- `ServerId` 必填，引用已有后端参数或资源。
- `Port` 必填，1~65535。
- `Weight` 取 0~100。
- 可选 `Type`（`ecs`、`eni`）、`Description`、`ServerIp`。

单次最多 20 台后端。监听选择虚拟服务器组时，必须使用其 `VServerGroupId` 返回属性建立真实引用，不要写固定字符串。

## 合法返回属性与输出

| 资源类型 | 官方 `Fn::GetAtt` 属性 |
|---|---|
| `ALIYUN::SLB::LoadBalancer` | `LoadBalancerId`、`IpAddress`、`NetworkType`、`AddressType`、`AddressIPVersion`、`VpcId`、`VSwitchId`、`Bandwidth`、`MasterZoneId`、`SlaveZoneId`、`LoadBalancerName`、`LoadBalancerSpec`、`ResourceGroupId`、`OrderId`、`PayType` |
| `ALIYUN::SLB::Listener` | `LoadBalancerId`、`ListenerPortsAndProtocol`、`Arn` |
| `ALIYUN::SLB::BackendServerAttachment` | `BackendServers`、`LoadBalancerId` |
| `ALIYUN::SLB::VServerGroup` | `VServerGroupId`、`BackendServers`、`LoadBalancerId`、`Arn` |

- CLB 没有 ALB 专属的 `DNSName` 返回属性；Listener 也没有 `ListenerId` 返回属性，不要虚构这些输出。
- 输出 `Value` 必须精确指向用户指定资源的合法 `Ref` 或官方 `Fn::GetAtt`，不要输出固定 ID、固定字符串或未声明资源的属性。
- 常用输出：负载均衡 `LoadBalancerId`/`IpAddress`，监听 `ListenerPortsAndProtocol`，虚拟服务器组 `VServerGroupId`/`BackendServers`，默认后端 `BackendServers`/`LoadBalancerId`。

## 交付前必要检查

1. `ROSTemplateFormatVersion`、资源类型、资源逻辑 ID、参数名、参数 `Type`、`Default` 限制和输出名与用户要求一致。
2. 私网 IPv4 实例显式包含 `AddressType="intranet"`、`AddressIPVersion="ipv4"`，并正确引用 `VpcId`、`VSwitchId`。
3. `Protocol` 使用 CLB 小写枚举；`https` 提供参数化的 `ServerCertificateId`；字符串开关与 Boolean 字段类型正确。
4. `HealthCheck`、`Persistence` 保持 Map 嵌套路径，内部键与协议适配正确；数组在父属性处整体表达。
5. 监听、默认后端或虚拟服务器组与负载均衡实例之间存在真实 `Ref`/`Fn::GetAtt` 引用，后端 ID 和证书 ID 不硬编码。
6. 每个 `Fn::GetAtt` 的资源已声明且属性在官方返回属性表内；没有 `DNSName`、`ListenerId` 等虚构输出。
7. 已有模板修改仅触及用户授权的对象路径，并保留其余结构、命名、顺序和元数据。

以上检查不等于云侧部署或运行时验收。若需要库存、配额、证书权限、实际健康检查结果或流量验证，必须另行执行相应云侧验证。

## 工具选择、查找顺序与重试

1. 先定型操作：新模板按“快速导航与资源选择”选择资源组合；局部修改先读取原始模板、实际资源类型、逻辑 ID 和授权对象路径，不做全局 schema 检索或无关扩展读取。
2. 字段资料按序取用：任务显式要求 → 本参考或随任务提供的已核对官方摘要 → 目标资源类型与目标 ROS 版本的官方 ROS schema。不要用 SLB API/SDK、控制台示例或 ALB/NLB 文档推断 ROS 层级、必填性或 `Fn::GetAtt` 属性。
3. 具体字段争议优先查看对应模板片段、差异或已有对象路径；已有结构化结论时，不读取完整轨迹。只有出现具体失败、未支持结论或资料冲突时，才读取与该问题直接相关的最小证据片段。
4. 本文未覆盖字段、版本差异或校验报错时，查询目标官方 schema；官方 URL 是来源记录，离线使用本文不必访问网络。无法确认就明确说明，未确认不等于取值为空，也不能默认使用可选字段或编造返回属性。
5. 官方文档查询出现临时网络或解析错误时，用精确资源标题、属性名或错误码重试一次；仍失败则记录未确认，不切换到非 ROS 资料来源。资料冲突以目标资源类型和目标 ROS 版本的官方 schema 为准。

## 官方来源

- `https://help.aliyun.com/zh/ros/developer-reference/aliyun-slb-loadbalancer`
- `https://help.aliyun.com/zh/ros/developer-reference/aliyun-slb-listener`
- `https://help.aliyun.com/zh/ros/developer-reference/aliyun-slb-backendserverattachment`
- `https://help.aliyun.com/zh/ros/developer-reference/aliyun-slb-vservergroup`
