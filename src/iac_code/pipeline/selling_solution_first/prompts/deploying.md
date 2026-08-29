# 步骤：部署

你正在执行「先选方案，再实现方案」流程的最终步骤。部署、参数补全、可用性检查、等待和失败恢复规则以共享的 `iac-aliyun-deploying` 技能为准；本 prompt 只适配新 pipeline 的确认门禁、上下文和回滚目标。

## 已确认方案与门禁

```json
{selected_plan}
```

用户已在上一步通过专用部署确认交互授权当前方案。不要再次询问是否部署或是否确认参数。

- `selected_plan.deployment_gate_valid` 为 `true` 时才允许调用 `ros_deploy`。
- 门禁为 `false` 时不得调用部署工具：确认、参数或模板交接不完整则回滚到 `materialize_selected_candidate`；产品组合或架构必须改变则回滚到 `solution_planning_and_selection`。reason 使用 `selected_plan.deployment_gate_error`。
- `selected_plan.selection_valid` 为 `false` 时回滚到 `materialize_selected_candidate`，reason 使用 `selected_plan.selection_invalid_reason`。

## 部署输入

- 方案：`{solution_selection.selected_candidate.name}`
- 模板：`{selected_plan.template_url}`
- 参数以 `selected_plan.effective_deployment_parameters` 为基础，叠加 `selected_plan.parameter_overrides`，其余装配和恢复遵循技能。
- `preview_ready_for_create: true` 时走技能的快速创建路径，否则走常规路径。

需要校验或执行创建类动作时，`template_url` 必须是 `{selected_plan.template_url}`；`wait` 不传模板。模板只能在该路径就地修复，不得改用新文件。部署生命周期只通过 `ros_deploy`，不得绕过 wrapper 调用其它 ROS 写接口。

前序方案上下文仅用于理解已确认方案，不得据此改写部署目标：

```json
{solution_selection}
```

## 完成与回滚

- 部署成功后只提交 `{"conclusion":{"status":"success"}}`；Python 从最新真实 `CREATE_COMPLETE` 记录注入 stack_id 和 outputs。
- 最终失败只提交 `status: failed`，Python 从真实失败记录注入 error；取消只提交 `status: cancelled`。
- `complete_step` 成功后只渲染刚提交的真实 Stack Outputs，不要再次提交。
- 架构层面必须改变时回滚到 `solution_planning_and_selection`；其它模板、参数和部署失败按技能恢复。
- 不读取项目文件或记忆，不在本 Step 重新询价。
