# 费用估算

相关选型：[产品指南](knowledge://clouds/aliyun/products/ecs/ecs.md)。

## 费用估算

- 通过 `aliyun_api` 调用 [DescribePrice](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeprice)（`ResourceType: instance`），按目标地域、规格、镜像和磁盘配置查价，计费方式与周期须与方案一致。
- 系统盘参数使用 `SystemDisk.*`；资源清单中的 ECS 费用须包含所配磁盘，或单列磁盘费用，避免漏算或重复计费。
- 合计采用本账号 **`TradePrice`（优惠后最终价）**，`OriginalPrice`（原价）单列参考；`DiscountPrice` 是减免金额，不是应付价。
- 按量用 `PriceUnit: Hour`、`Period: 1` 询价后按使用时长换算，月预算默认 720 小时并注明假设；包年包月才用 `Week/Month/Year` 及对应 `Period`。公网费用另计，未知费用待确认。
