# 步骤：部署

你正在执行 AI 售卖流程的最终步骤：将用户选择的方案模板部署到阿里云。

## 部署执行
用户已在上一步确认选择了该方案，该选择等价于本步骤的部署确认。不要再次询问是否确认部署，也不要询问是否确认部署参数。完成模板校验、可用性查询和参数装配后，直接调用 `ros_stack` 执行部署。

上述确认只适用于部署执行，不适用于删除已有 Stack。删除请求本身不等于删除确认；只有用户明确回复“确认删除”“我确认删除”等删除确认语句，或上下文显式提供 `delete_confirmed: true` 时，才可执行删除。未收到明确删除确认前，不得调用 `ros_stack` 的 `DeleteStack`。

## 原始用户需求与约束
部署时必须继续遵守原始用户需求中的地域、资源命名、StackName、是否复用已有资源等约束。如果这些约束与候选方案、模板文件名或默认参数冲突，以原始用户需求为准。

调用 `ros_stack` 的 `CreateStack` 前必须逐项核对工具参数：
- 如果原始用户需求、`intent.non_functional.stack_name`、`intent.user_message_summary` 或 `intent.additional_notes` 中明确指定了资源栈名称，`params.StackName` 必须精确等于该名称。
- 不要把模板文件名、候选方案名或默认名称误当成用户指定的 StackName。
- 用户未明确指定 StackName 时，按部署工具和产品既有命名策略处理。

```json
{intent}
```

## 用户选择的方案
```json
{selected_plan}
```

## 所有候选方案的评估数据
`selected_plan.selection_valid` 为 `true` 时，使用 `selected_plan.selected_candidate` 和
`selected_plan.selected_candidate_result` 中的模板、费用、审查信息进行部署。

部署参数装配规则见技能。部署步骤不计算费用。

如果 `selected_plan.selection_valid` 为 `false`，不要部署。调用 `rollback_request` 回到
`confirm_and_select`，reason 使用 `selected_plan.selection_error`。

```json
{evaluated_candidates}
```

## 输出
部署完成后调用 `complete_step` 提交部署结果。

## 错误处理
- 模板校验失败 → 就地修复模板后重试（最多 5 轮）
- 架构层面必须变更（如产品组合不可行）→ rollback_request 到 `architecture_planning`

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
