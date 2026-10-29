# 阿里云轻量应用服务器（SWAS）选型指南

## 适用场景

- 适合个人网站、开发测试、小型 Web 和单机应用，优先考虑标准套餐及应用镜像，减少环境搭建工作。
- 需要 GPU、复杂网络、弹性伸缩或更丰富实例规格时，选择 ECS。
- SWAS 是独立产品，底层基于 ECS 的计算、存储和网络资源；API 产品标识为 `SWAS-OPEN`，套餐、镜像和防火墙使用 SWAS 自己的标识与接口。

## 套餐选择

根据目标地域下的实际套餐，选择满足应用要求的配置：

| 维度 | 选择依据 |
|------|----------|
| 计算 | `Core`（vCPU）、`Memory`（GiB）：满足应用及同机运行服务的 CPU、内存需求 |
| 存储 | `DiskSize`（GiB）、`DiskType`：系统、应用与数据所需容量及磁盘类型 |
| 网络 | `Bandwidth`（Mbps）、`Flow`（GB/月）：峰值带宽与每月流量配额，结合访问量选择 |
| 操作系统 | `SupportPlatform`：套餐支持的 Linux / Windows 平台 |
| 价格 | `OriginPrice`、`Currency`：套餐包月参考价与币种 |

以查询结果关联 `PlanId` 与规格，不根据套餐 ID 猜测配置。不同地域的套餐及镜像可能不同，不直接复用其他地域的选择。

`OriginPrice` 返回字符串金额，计算费用时按数值处理；`SupportPlatform` 是 JSON 数组形式的字符串（如 `["Linux","Windows"]`），需解析后匹配平台。`Flow` 可能缺失，不将缺失值直接解释为零流量或无限流量；结合当前套餐说明判断，信息不足时保留未知。

## 镜像选择

- 标准应用或容器环境：优先匹配应用镜像，例如 WordPress、Docker；核对镜像描述中的系统与组件版本是否适配应用。
- 自定义技术栈：选择兼容的系统镜像，自行安装运行环境。
- 复制已有环境：可使用轻量应用服务器的自定义镜像。
- 使用 `ListImages` 返回的 SWAS `ImageId`，不要直接使用 ECS 镜像 ID，也不要把教程中的旧版本或示例 ID 当成当前可用值。
- 镜像列表可能包含旧版本，优先选择仍受维护且满足应用依赖的版本，不按返回顺序直接取第一项。

## 存储与网络配置

- 套餐已包含系统盘和公网访问能力，不要为同一能力额外配置 ECS 系统盘或 EIP。
- 应用数据超过系统盘容量时，可增加一块单独计费的数据盘；每台实例最多挂载一块，数据盘不能合并到系统分区。需要扩大系统盘时升级套餐。
- 实例使用 SWAS 防火墙控制入流量。按最小授权原则配置业务端口与来源地址，不套用 ECS 安全组。
- 流量政策按套餐区分：固定流量配额套餐可能产生超额流量费，无固定流量套餐按其计费说明处理；不要仅凭峰值带宽承诺无限流量。

## 计费与价格

- 套餐和数据盘按包年包月购买；适用的超额流量按量计费。
- `ListPlans.OriginPrice` 是套餐的**包月价格**。参考总价可按「包月价格 × 购买月数 × 台数」折算，但该值不包含年付折扣、活动优惠或额外数据盘等费用。
- 包月价、年付总价和活动价分别说明，比较时保持地域、套餐、购买时长、数量及费用范围一致。
- 首购、学生、新人或续费等活动价格受活动条件约束，不凭规格相同就认定用户可享受同一优惠；实际费用以当前购买或询价结果为准。

## 套餐与镜像查询

### 查询目标地域的套餐

```python
aliyun_api(product="SWAS-OPEN", action="ListPlans", region_id="<目标地域>")
```

根据套餐字段筛选配置，保留对应的 `PlanId`、价格及币种。套餐列表描述规格和参考价格，不代表已经完成库存或创建可行性检查。

### 查询目标地域的镜像

```python
aliyun_api(product="SWAS-OPEN", action="ListImages", params={
    "ImageType": "system"
}, region_id="<目标地域>")
```

`ImageType` 可选 `system`（系统镜像）、`app`（应用镜像）或 `custom`（自定义镜像）；根据镜像的 `Platform`、名称和描述匹配套餐平台与应用依赖。

## 最佳实践

- 应用镜像能够满足需求时，优先复用预装环境；自定义部署应匹配操作系统与依赖版本。
- 网站或 API 仅开放必要业务入口；管理端口限制来源，数据库等内部服务避免直接向公网开放。
- 重要数据保留备份；重置系统前备份系统盘数据，重置会清除原有系统盘内容。
- 持续监控资源使用情况，容量不足时考虑升级套餐。套餐仅支持升配、不支持降配，也不支持单独升级 CPU、内存、系统盘或带宽；需要更复杂资源配置时评估迁移到 ECS。

## 官方参考

- [产品概述](https://help.aliyun.com/zh/simple-application-server/product-overview/what-is-simple-application-server)
- [与 ECS 对比](https://help.aliyun.com/zh/simple-application-server/product-overview/comparison-between-simple-application-server-and-ecs)
- [ListPlans：套餐信息与包月价格字段](https://help.aliyun.com/zh/simple-application-server/developer-reference/api-swas-open-2020-06-01-listplans)
- [ListImages：镜像信息](https://help.aliyun.com/zh/simple-application-server/developer-reference/api-swas-open-2020-06-01-listimages)
- [计费项](https://help.aliyun.com/zh/simple-application-server/product-overview/billable-items)
- [挂载数据盘及使用限制](https://help.aliyun.com/zh/simple-application-server/user-guide/attach-a-data-disk)
- [升级套餐配置及限制](https://help.aliyun.com/zh/simple-application-server/user-guide/upgrade-a-simple-application-server)
- [最佳实践：快速部署 Docker](https://help.aliyun.com/zh/simple-application-server/use-cases/deploy-and-use-docker)
- [管理防火墙](https://help.aliyun.com/zh/simple-application-server/user-guide/manage-the-firewall-of-a-server)
