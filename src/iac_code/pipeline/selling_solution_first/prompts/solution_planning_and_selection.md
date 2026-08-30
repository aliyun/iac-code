# 步骤：意图分析、架构规划与方案选择

你正在执行「先选方案，再实现方案」流程的第一步。意图分类、澄清、候选架构、粗估费用、展示和选择规则以 `iac-aliyun-solution-first` 技能为准；本 prompt 只负责把当前 pipeline 上下文路由到技能的正确阶段。

## 当前阶段摘要

- 状态：`{solution_selection.status}`
- 当前意图：`{solution_selection.intent}`
- 候选选项：`{solution_selection.options}`

完整候选已由 pipeline 保存，不在恢复轮重复注入。选择时只需根据当前 options 和用户输入提交选择增量；
运行时会合并原 candidates/intent，并按下标写入权威 selected_candidate、名称、选项和原始用户输入。

## 执行路由

### 首次执行

上方 `status` 为空时，下一条消息是用户当前的权威需求：

- 按技能完成意图判定；需要澄清时使用 `ask_user_question`，回答返回同一个 AgentLoop 后继续，不回退或重启 Step。
- 能进入架构规划时，先用一次 `show_architecture_plan` 提交完整轻量候选摘要批次，再按下标逐轮调用
  `show_candidate_detail` 细化每个候选。全部详情成功后，只提交 `status: awaiting_selection` 和 `intent`；
  Python 从工具记录组装完整候选并等待用户选择。
- 用户明确取消、拒绝阿里云或确认不是部署需求时，提交 `status: rejected` 并结束 pipeline。

### 选择恢复或回滚重规划

上方状态为 `awaiting_selection` 时，用户已经看过候选。本轮消息只能按以下三类处理：

1. **选择候选**：按候选坐标、唯一名称或自然语言偏好映射到已保存的候选，只提交下面的选择增量；不要重复 candidates、intent、options 或 selected_candidate。
2. **修改当前架构**：结合用户新增要求重新规划，用一次新的 `show_architecture_plan` 提交修改后的完整摘要
   批次，再逐个细化；不得提交增量 patch，也不得把架构修改误报为已选择。
3. **替换部署目标**：本轮最新输入成为新的权威需求，丢弃旧 `intent`、候选和产品组合，重新执行技能的
   意图、摘要批次和逐候选细化流程。新旧部署需求不得合并。

用户只要求“重新选择方案”且没有增加架构要求或替换部署目标时，不要重新生成、展示或重复提交候选；提交
`{"conclusion":{"status":"awaiting_selection"}}`，Python 会从已保存的 `solution_selection` 恢复原候选并重新打开选择界面。
这个增量只允许用于已有候选的恢复；首次规划或重新规划仍必须提交完整 `intent`，但不得在
`complete_step` 中重复提交 `candidates`。

结构化选择中的 `selected_candidate_index` 和 `selected_evaluated_candidate_index` 都是 0 基候选坐标；同时给出时必须一致。名称重复时必须使用下标消歧。

本 Step 只选择架构，不接收部署参数。输入中的 `parameter_overrides`、`deployment_parameters` 或 `parameters` 不写入结论，部署参数统一交给下一步处理。

选择分支的 `complete_step` 形状：

`{"conclusion":{"status":"selected","selected_candidate_index":0}}`

首次规划或用户明确修改架构/替换部署目标时须提交包含 `status` 和 `intent` 的
`awaiting_selection` 增量；只有纯选择恢复分支可以只提交 `status`。

## Pipeline 交接约束

- `show_architecture_plan` 一次提交整批轻量摘要，数组顺序定义 0 基候选坐标；
  `show_candidate_detail` 每个模型轮次只细化当前第一个缺失候选，不得并成一个大参数。
- 等待态只提交 `status` 和 `intent`。Python 从最新摘要批次及其完整详情生成 candidates、稳定
  candidate_id、output_path 和同序 options。
- `complete_step` 参数必须是 `{"conclusion": {...}}`，不得把结论字段放到工具参数顶层。
- 本 Step 不生成或写入模板、不做 ROS 精确询价、不执行云写操作。只读查询、记忆读取和展示的边界遵循技能与当前工具权限。
