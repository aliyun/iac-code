# 阿里云全球加速 GA 选型指南

## 全局规则

- 本文只固化产品选型和安全边界。资源属性、枚举、支持的终端节点类型、地域覆盖和账号资格可能变化，**每次生成前必须查询当前 Schema 和可用性 API**。
- ROS Schema：`aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})`。
- Terraform Schema：`aliyun_api(product="IaCService", action="GetResourceType", style="ROA", method="GET", pathname="/resourceType/<类型>")`。
- 下文资源名是查询 Schema 的定位锚点，不代表当前版本一定支持全部能力。只有 Schema 返回完整资源链和准确属性后才能生成；缺失时说明限制并询问是否切换 IaC 实现，不得编造属性或用其他资源冒充。
- Anycast、自定义路由、WAF、跨境线路、高防、大规格和特定 ISP 等受限能力，生成前确认当前地域、账号资格与产品约束；未确认不得默认启用。
- 证书、已有资源 ID、外部源站 IP/域名均由用户提供或从真实 API 结果中选择，禁止编造。

## 默认选型顺序

用户未指定时，按以下顺序推荐：

1. **标准型按量付费**：默认选择，适合大多数 TCP、UDP、HTTP、HTTPS 跨地域加速场景。
2. **标准型包年包月**：仅在流量长期稳定、希望按固定带宽付费时选择。
3. **基础型**：仅在明确需要三层点到点加速、自定义协议或源站主动访问公网，且一个加速区域对应一个终端节点组时选择。

不得仅因成本原因把标准型静默降级为基础型；二者的协议层级、拓扑和能力不同。

| 需求 | 推荐形态 |
|------|----------|
| 网站、API、企业应用、AI 服务、通用 TCP/UDP | 标准型 + 智能路由 |
| 按监听端口确定性映射到指定后端 IP/端口 | 标准型 + 自定义路由 |
| 三层点到点、私有协议、源站主动访问公网 | 基础型 |
| 海外用户分散且需要统一入口 IP | 标准型 + Anycast；先确认当前覆盖和资格 |

## 付费模式映射

ROS 与 Terraform 的字段名和枚举不同，不能通过大小写或 snake_case 机械转换：

| 形态 | ROS | Terraform | 说明 |
|------|-----|-----------|------|
| 标准型按量付费（默认） | `InstanceChargeType: POSTPAY`、`BandwidthBillingType: CDT` | `payment_type = "PayAsYouGo"`、`bandwidth_billing_type = "CDT"` | 无需基础带宽包 |
| 标准型包年包月 | `InstanceChargeType: PREPAY`、`BandwidthBillingType: BandwidthPackage` | `payment_type = "Subscription"`、`bandwidth_billing_type = "BandwidthPackage"` | 创建并绑定基础带宽包 |
| 基础型 | ROS 使用 `ChargeType`；Terraform 使用 `payment_type` | 取值按上述按量/包年包月映射 | 不得生成 Terraform `charge_type` |

付费模式创建后不得在模板更新中静默切换。`Spec`、价格、可售带宽类型和账期约束不在本文固化，以当前 Schema、询价和售卖能力为准。

## 接入与路由决策

### EIP 与 Anycast

| 接入方式 | 稳定选型规则 |
|----------|--------------|
| EIP 自定义就近接入 | 需要指定加速地域、按地域控制入口或分配带宽时使用；标准型地域位于 `IpSets.AccelerateRegion` 列表内 |
| Anycast 自动就近接入 | 需要海外多地域统一入口 IP 时考虑；不配置独立加速地域，生成前查询当前 IaC Schema、覆盖和资格 |

若所选 IaC Schema 未返回 Anycast 接入属性，说明限制并改用当前支持的 ROS、Terraform 或 OpenAPI 方案；不得凭经验编造 `ip_set_config`、`access_mode` 等字段。

### 智能路由（默认）

- 根据时延、健康状态和权重转发，适用于绝大多数标准协议场景。
- ROS 从 `ALIYUN::GA::Listener` 查询当前监听属性；Terraform 从 `alicloud_ga_listener` 查询。不得根据经验猜测 `Type`、`ListenerType` 或 `listener_type`。
- 监听协议必须匹配业务；HTTPS 引用真实证书并选择当前 Schema 支持的 TLS 策略。
- ROS 智能路由终端节点组以 `ALIYUN::GA::EndpointGroup` / `EndpointGroups` 为查询锚点；Terraform 以 `alicloud_ga_endpoint_group` 为查询锚点。根据 Schema 返回的终端节点类型和必填字段选择资源，不固化当前类型清单。

### 自定义路由

- 仅用于分房游戏、音视频等“监听端口 → 指定后端地址和端口”的确定性映射场景。
- 同一 GA 实例不能混用智能路由和自定义路由；监听路由类型创建后不能变更，切换时重建监听及关联资源。
- 终端节点使用 VSwitch ID（`vsw-...`）；后端 IP、协议、端口和放行策略由 Schema 中的目的端口与流量策略资源表达，不能把 CIDR 填入 VSwitch ID 属性。
- 自定义路由不使用智能路由的健康检查。生成前分别查询监听、终端节点组、终端节点、目的端口和流量策略的完整资源链；链路不完整时报告限制，不能用普通智能路由终端节点组代替。

## 高频场景

| 场景 | 选型要点 |
|------|----------|
| 加速指定 IP、域名、网站或 API | 标准型按量付费 + 智能路由；加速地域靠近客户端，终端节点组地域与源站一致 |
| DSW、模型或镜像下载、Model Studio API | 标准型智能路由；使用真实公网域名，固定调用方时仅放行其 NAT/出口 IP |
| Web 业务接入 WAF | 标准型 HTTP/HTTPS；先确认当前支持地域、付费模式和账号资格 |
| 海外多地域统一 IP | 标准型 + Anycast；先查询当前覆盖与 IaC Schema |
| SSL-VPN | 监听协议和端口与 VPN 一致；GA 只加速传输，不替代 VPN 加密 |
| 分房游戏、确定性端口映射 | 标准型自定义路由；仅在所选 IaC Schema 返回完整资源链时生成 |

## 客户端源 IP、安全与高可用

- HTTP/HTTPS 通过 `X-Forwarded-For` 获取真实客户端 IP，后端正确解析代理链。
- TCP 仅在后端支持时启用 Proxy Protocol 或 Schema 返回的源 IP 保留能力，否则会导致协议解析失败。IPv6 客户端访问 IPv4 源站时，UDP 不保证保留 IPv6 源 IP。
- 监听只开放业务所需端口；固定客户端使用 ACL 白名单；HTTPS 证书作为外部输入。
- 涉及中国内地 Web 服务时检查 ICP 备案；涉及跨境网络时依据用户资质和当前可用线路生成，不猜测资质。
- 健康检查、终端节点权重和终端节点组流量比例仅用于**标准型智能路由**。生产环境默认开启健康检查；HTTP(S) 使用专用健康路径，TCP 使用 TCP 探测，具体参数以源站约束和当前 Schema 为准。
- 基础型和自定义路由不套用上述健康检查或权重属性，使用业务探测或独立监控设计容灾。

## IaC 资源定位

| 能力 | ROS 查询锚点 | Terraform 查询锚点 |
|------|--------------|--------------------|
| 标准型实例 | `ALIYUN::GA::Accelerator` | `alicloud_ga_accelerator` |
| 监听 | `ALIYUN::GA::Listener` | `alicloud_ga_listener` |
| EIP 加速地域 | `ALIYUN::GA::IpSets` | `alicloud_ga_ip_set` |
| 智能路由终端节点组 | `ALIYUN::GA::EndpointGroup` / `EndpointGroups` | `alicloud_ga_endpoint_group` |
| 转发规则与 ACL | `ALIYUN::GA::ForwardingRules`、`Acl`、`AclsListenerAssociation` | 查询 `alicloud_ga_*` 当前对应资源 |
| 基础带宽包及绑定 | `ALIYUN::GA::BandwidthPackage`、`BandwidthPackageAcceleratorAddition` | `alicloud_ga_bandwidth_package`、`alicloud_ga_bandwidth_package_attachment` |
| 基础型 | `ALIYUN::GA::Basic*` | `alicloud_ga_basic_*` |
| 自定义路由 | 查询当前 `ALIYUN::GA::*CustomRouting*` 及关联 Schema | 查询当前 `alicloud_ga_custom_routing_*` 完整资源链 |

资源名仅用于定位。选定 IaC 后，只生成该 Schema 当前返回的资源、属性、枚举和必填字段；不存在直接映射时报告差异，不做名称推导。

## 模板参数

| 参数 | 规则 |
|------|------|
| 标准型 EIP 加速地域 | ROS 位于 `IpSets.AccelerateRegion[].AccelerateRegionId`，不是 `IpSets.Properties` 顶层属性 |
| 基础型加速地域 | 从 `BasicIpSet` 当前 Schema 查询 `AccelerateRegionId` |
| `EndpointGroupRegion` | 与源站实际地域一致 |
| 源站 ID / IP / 域名 | 使用已有资源或外部输入，禁止编造 |
| HTTPS 证书 ID | 由用户提供或选择已有证书 |

监听协议、端口、IP 版本和带宽建议参数化；业务已明确时可设置有依据的默认值和 `AllowedValues`。

## 运行时查询与筛选

GA OpenAPI 使用文档指定的控制面地域（当前为 `cn-hangzhou`，调用前以最新 API 文档为准）。

| 用途 | API | 关键输入 |
|------|-----|----------|
| 标准型加速地域 | `ListAvailableAccelerateAreas` | 可选 `AcceleratorId`；查询 Anycast 时按当前 API 传 `AccessMode` |
| 终端节点可用地域 | `ListAvailableBusiRegions` | 已有实例时传 `AcceleratorId` |
| 公网线路类型 | `ListIspTypes` | `BusinessRegionId`、实例类型和可选实例 ID |
| 源站验证 | ECS、VPC、SLB、OSS 等对应产品 API | 资源 ID、地域、VPC/VSwitch、地址 |

筛选顺序：

1. 用户指定的实例类型、付费模式、地域、协议、端口、IP 版本和源站类型均为硬约束。
2. 查询所选 IaC 的当前资源 Schema，确认完整资源链、必填属性和枚举。
3. 查询加速地域、终端节点地域和线路的当前可用性，并验证源站资源与地域一致。
4. 能力需要额外资格或 Schema 缺失时，报告限制并给出可选实现；不得静默换 IaC、地域、协议或产品类型。
5. 没有满足全部硬约束的组合时，说明冲突并请用户调整约束。
