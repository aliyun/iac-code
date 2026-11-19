# EIP 费用估算

## EIP 费用估算

- 公网网络费按流量或固定带宽计费，IP 保有费作为固定费用列出；BGP EIP 直接绑定专有网络 ECS/ECI 且主账号 EIP 配额 ≤2000 时免保有费，须注明免收条件。流量由 CDT 结算时按 CDT 规则估算。
- 按需通过 `aliyun_api` 调用 `BssOpenApi`（`2017-12-14`）的 `DescribePricingModule` → `GetPayAsYouGoPrice`；传 `ProductCode: eip`、`ProductType: ""`（不可省略）、`SubscriptionType: PayAsYouGo`，按返回的模块配置和目标地域的定价编码组装询价参数。
- 采用返回模块的 **`CostAfterDiscount`（优惠价）**，`OriginalCost`（原价）单列参考；`InvoiceDiscount` 是折扣金额，不是应付价。核对小时、天、GB 等单位，不混加不同单位的金额，也不因 `UnitPrice: 0` 就判为免费。

详细参数与计费条件参考：[后付费询价](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-getpayasyougoprice)、[EIP 计费规则](https://help.aliyun.com/zh/eip/pay-as-you-go/)。
