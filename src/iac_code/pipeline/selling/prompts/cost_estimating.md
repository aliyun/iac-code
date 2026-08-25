# 步骤：成本预估

你正在为候选方案预估部署费用，具体参数求解、PreviewStack 尝试和软降级规则按技能执行。不要使用 `ros_stack` 执行预览。

在记录参数缺口前，必须先尽量补齐可生成参数，尤其是普通密码；生成值须满足模板约束，并以同一个真实值写入预览、询价、`deployment_parameters`、`preview_validation.parameters` 和 `complete_step.conclusion`，不得写入 `***`、`[REDACTED]` 或 `<redacted>`。库存值和外部输入不得编造，仍按缺口记录。服务端日志由运行时单独脱敏，不要因此改写结构化交接数据。

## 当前候选方案
- 名称：`{candidate.name}`
- 资源生命周期：
```json
{candidate.resource_intents}
```

## 当前有效硬约束
```json
{candidate.hard_constraints}
```

## 架构规划阶段的粗估（用于对账）
- 粗估月费：`{candidate.monthly_estimate}`
- 该值只是规划阶段的粗略区间，不是最终价格；本步骤的询价结果才是权威口径。

## 模板信息
- 文件路径：`{template.file_path}`
- 地域：`{template.region}`

## ROS 模板来源
- 本步骤的模板文件路径已经确定为 `{template.file_path}`。
- 查询参数约束时调用 `ros_get_template_parameter_constraints`，必须传 `template_url = "{template.file_path}"`，可选 `parameters` 传当前已知参数字典。
- 预览模板时调用 `ros_preview_template`，必须传 `template_url = "{template.file_path}"`、`stack_name` 和 `parameters` 字典。
- 询价时调用 `ros_estimate_template_cost`，必须传 `template_url = "{template.file_path}"` 和 `parameters` 字典。
- 如果修复模板后需要校验，调用 `ros_validate_template`，必须传同一个 `template_url = "{template.file_path}"`。
- 不要调用 `aliyun_api` 的 ROS `GetTemplateParameterConstraints`、`PreviewStack`、`GetTemplateEstimateCost` 或 `ValidateTemplate` 接口；不要传 `TemplateBody`、`TemplateId` 或 `TemplateScratchId`。

## 禁止事项
- **不要**自行估算费用
- **不要**搜索定价文档
- **不要**使用 aliyun_doc_search

## 输出
API 调用完成后调用 `complete_step` 提交费用预估。

`complete_step.conclusion.monthly_estimate` 必须保留两个价格口径，且统一使用**按量付费列表价折算月度**口径（与 architecture_planning 粗估口径一致）：
- `OriginalAmount` 是原价，按统一月度周期换算并汇总为列表价，是主口径。
- `TradeAmount` 是合同优惠后的最终价，按与原价相同的月度周期换算并汇总。
- 两个字段都存在时，使用 `¥<原价>/月（列表价，合同优惠后约¥<最终价>/月）` 格式；即使数值相同也保留两个价格口径。
- 任一字段缺失时只展示可用价格，并在 `api_raw_summary` 中说明缺失字段；询价失败时仍填写 `"询价失败"`。
- 不要把包年包月价格与按量付费价格混在同一字段中呈现。

`complete_step.conclusion.pricing_provenance` 必须说明价格来源：
- `caliber` 固定为 `pay_as_you_go_monthly`；`list_price_source` 写明列表价来源（如 `GetTemplateEstimateCost.OriginalAmount`）。
- 展示了合同优惠价时，必须写明 `contract_price_source`（如 `GetTemplateEstimateCost.TradeAmount`）；无法说明折扣依据时，把 `contract_price_is_estimate` 置为 `true` 并在 `monthly_estimate` 中标注「估算」。缺少来源又未标注估算时，代码会拒绝结束步骤。

`complete_step.conclusion.planning_deviation` 必须记录与上方规划粗估的对账结果：
- 最终列表价落在粗估区间（含 20% 容差）内 → `status: aligned`。
- 显著超出区间，或模板生成阶段改变了规格 → `status: deviated`，用 `spec_changes` 列出规格变更（`item/planned/actual`），并在 `reason` 说明变更原因。不得静默替换数值。
- 粗估无法解析出金额时才用 `status: planning_estimate_unavailable`。

若 `ros_preview_template` 成功，在 `complete_step.conclusion.preview_validation` 写入 PreviewStack 成功证明：`succeeded: true`、`template_url: "{template.file_path}"`、`parameters: <预览通过的同一参数字典>`；失败或未执行时写入 `succeeded: false`、`error: "<原因>"`。

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
