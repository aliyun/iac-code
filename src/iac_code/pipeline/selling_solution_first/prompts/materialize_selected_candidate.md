# 步骤：实现用户选中的方案

你正在执行「先选方案，再实现方案」流程的第二步。模板生成、参数求解、Preview、ROS 精确询价、方案说明和部署确认规则以 `iac-aliyun-materialize-selected-candidate` 技能为准；本 prompt 只提供当前选中方案、恢复状态和 pipeline 控制流。

## 用户选中的方案

```json
{solution_selection.selected_candidate}
```

- 方案名称：`{solution_selection.selected_candidate_name}`
- 模板路径：`{solution_selection.selected_candidate.output_path}`

## 前序意图

```json
{solution_selection.intent}
```

## 当前物化摘要

- 状态/模板：`{selected_plan.status}` / `{selected_plan.template_url}`
- 覆盖/最终参数：`{selected_plan.parameter_overrides}` / `{selected_plan.effective_deployment_parameters}`
- 方案说明：`{selected_plan.selected_candidate_result.solution_summary}`
- 询价概览/明细：`{selected_plan.selected_candidate_result.cost.monthly_estimate}` / `{selected_plan.selected_candidate_result.cost.resources}`
- 参数缺口：`{selected_plan.selected_candidate_result.cost.missing_deployment_parameters}` / `{selected_plan.selected_candidate_result.cost.user_required_missing_parameters}`

模板正文只保存在文件，规范化价格、参数和确认等待态由 pipeline 保存。恢复轮使用干净的模型上下文；需要检查模板时读取
`template_url` 文件，不要要求把旧工具历史重新注入。

## 执行路由

### 选择无效

`{solution_selection.selection_valid}` 为 `false` 时不要生成模板。提交 `status: reselect_requested` 和具体原因；Python 会生成回到 `solution_planning_and_selection` 的外层 rollback_request。

### 首次物化

当前物化状态为空时，只实现上方唯一候选，并按技能顺序完成模板、校验、参数、Preview、询价和确认等待态。

模板从写入到确认始终使用同一路径 `{solution_selection.selected_candidate.output_path}`：

- `ros_validate_template`、`ros_get_template_parameter_constraints`、`ros_preview_template` 和 `ros_estimate_template_cost` 的 `template_url` 都绑定该路径。
- 模板错误只能就地修复该文件，不得另写替代文件。
- 最终部署确认使用 `deployment_confirmation` 等待态，不使用 `ask_user_question`；后者只用于补齐用户外部必填参数或澄清含糊输入。

提交 `status: awaiting_confirmation` 后不要再用普通助手文本重复方案、价格、Preview 或参数，界面会从结构化 conclusion 统一展示。

### 确认恢复

`selected_plan.status` 为 `awaiting_confirmation` 时，本轮消息是用户对当前完整方案的操作：

- 可解析的结构化 `action` 必须按技能确定性处理；结构化 `confirm` 由 Python 直接完成，不会交给你判断。
- 非结构化输入由 LLM 按技能区分确认、取消、当前参数调整、架构变化或全新部署目标。
- 确认沿用当前模板、参数、Preview、询价和方案说明，只提交 `status: confirmed`；不得重新执行物化工具或再次等待确认。携带新参数覆盖的确认同样只需一次，由 Python 合并并校验参数。
- 没有确认语义的参数调整请求留在本 Step，重新完成必要的参数约束、Preview、询价和方案说明后，再次提交 `status: awaiting_confirmation`。
- 架构变化或全新部署目标不修改当前模板，提交 `status: reselect_requested` 和完整 `reselect_reason`。Python 会固定回滚到 Step 1；新目标必须完整保留并替换旧意图。
- 取消提交 `status: cancelled` 并结束 pipeline。

## Pipeline 交接约束

- 候选身份只从 Step 1 的 `solution_selection.selected_candidate` 读取；不要在 Step 2 conclusion 中复制候选。
- `parameter_overrides: {}` 表示用户没有覆盖参数，是合法状态。
- 首次 `awaiting_confirmation` 只提交 `solution_summary`、`parameter_overrides`、参数缺口和精简硬约束判断。每条硬约束包含 LLM 独立 `status`；有可解析证据时提交 locator，工具已尝试但没有证据字段时提交空 `evidence`。Python 从真实工具记录生成 template、价格、Preview、最终参数和 UI 字段，并独立校验硬约束；LLM 或 Python 任一判断满足即放行。
- confirmed/cancelled 只提交 status；reselect_requested 再提交 reselect_reason。confirmation、取消原因和 rollback_request 由 Python 绑定本轮原始输入。
- 提交确认后不得再调用模板写入、校验、参数约束、Preview 或询价工具。
- `complete_step` 参数始终使用 `{"conclusion": {...}}`。
- 本 Step 不创建、更新或删除云资源；本地辅助命令也不得绕过该边界。

确认增量：

`{"conclusion":{"status":"confirmed"}}`

取消只提交 status；重新规划提交 status/reselect_reason。参数调整后重新执行 Preview 和询价，再提交新的
solution_summary、parameter_overrides、missing_deployment_parameters 与 hard_constraint_checks，不提交模板正文、价格或 Preview 复制。
