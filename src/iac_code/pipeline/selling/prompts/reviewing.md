# 步骤：审查并修复

你正在执行 AI 售卖流程的模板审查步骤。此步骤用 InfraGuard 审查上一步生成的 ROS 模板；如果初始扫描干净，不做额外校验和复扫，直接把模板继续作为 `template` 结论传给成本预估；如果发现需要修复的问题，则直接修复原模板文件。

## 模板信息
- 文件路径：`{template.file_path}`
- 地域：`{template.region}`
- 描述：`{template.description}`

## 用户意图
```json
{intent}
```

## 当前候选方案
```json
{candidate}
```

## InfraGuard step config
以下值由 `pipeline.yaml` 的当前步骤配置动态渲染。`aspects` 是可选审查维度目录，每个 aspect 下的 `policies` 由工具展开为 InfraGuard policy：

```json
{step_config.infraguard}
```

## 执行流程
1. 用 `read_file` 读取 `template.file_path` 指向的原模板文件。
2. 根据用户意图、当前候选方案和模板资源，选择适用的 aspect key。只能从 step config 的 `aspects` key 中选择；不要自己编写 policy id。记录 `selected_aspects` 和跳过 aspect 的原因。
3. 调用 `infraguard_scan`，参数来自上方 step config：`file_path={template.file_path}`，`mode`、`selected_aspects`、`aspect_policy_map=aspects`、`ignore_waivers`、`blocking_severities` 均使用渲染出的配置值，并设置 `include_file_content=true`。
4. 如果初始扫描 `passed=true`、`blocking_findings=0`，且没有需要修复的 finding，不要调用 `ros_validate_template`，不要再次调用 `infraguard_scan`；直接把这次扫描返回的 `file_content` 原样放入 `complete_step.conclusion.template`，把 `file_sha256` 放入 `complete_step.conclusion.template_sha256`。
5. 如果 InfraGuard findings 需要修改模板，直接修改原模板文件，使用 `edit_file` 或 `write_file` 写回同一个 `template.file_path`。
6. 只要本步骤修改过 `template.file_path`，就必须调用 `ros_validate_template(template_url=template.file_path)` 校验同一个文件路径。
7. 如 `ros_validate_template` 失败，分析错误并继续修复原模板文件；最多执行 `max_fix_rounds` 轮修复。
8. `ros_validate_template` 通过后，使用同一组 `selected_aspects` 再次调用 `infraguard_scan` 扫描同一个 `template.file_path`，并设置 `include_file_content=true`。
9. 如果本步骤没有改动模板，初始扫描就是最新可用扫描；如果改动过模板，最终扫描才是最新可用扫描。只有最新可用 InfraGuard 结果 `passed=true`、`blocking_findings=0`，且 `conclusion.template` 的 sha256 精确等于该扫描的 `file_sha256` 时，才能调用 `complete_step`。

如果任一轮 `infraguard_scan` 返回工具错误或错误 payload（如 `command_not_found`、`timeout`、`malformed_json`、`unexpected_exit_code`、`unknown_policy_aspect`），不要执行 policy update，不要跳过扫描继续完成，也不要输出 `validated=true` 或 `review_passed=true`。记录错误类型、`command`、`stderr` 摘要、`file_path`、`selected_aspects` 和已展开 policies（如果有），让 pipeline/prerequisites 或用户能处理该前置问题。

## 禁止事项
- 不要执行 InfraGuard 的 policy update 命令；策略更新由 pipeline prerequisites 的 post_install 负责。
- 不要默认使用 rollback_request 回到 `template_generating`；本步骤负责直接修复原模板文件。
- 不要把问题只放进单独的 `review` 字段；本步骤的 `conclusion_field` 是 `template`。
- 不要只报告 InfraGuard 标记为 blocking 的问题而不修复。
- 不要改写成新的文件路径；后续步骤必须继续读取同一个 `template.file_path`。
- 不要在没有 InfraGuard finding、`ros_validate_template` 错误或用户明确约束的情况下，按硬编码安全/合规/架构规则改写模板。
- 初始 InfraGuard 扫描已经通过且未修改模板时，不要为了“确认”而调用 `ros_validate_template` 或第二次 `infraguard_scan`。

## 输出
调用 `complete_step`，`conclusion` 必须包含：
- `template`: 最新文件内容，必须原样等于最新可用 `infraguard_scan` 返回的 `file_content`
- `template_sha256`: 最新模板内容的 sha256，必须等于最新可用 `infraguard_scan` 返回的 `file_sha256`
- `file_path`: 与输入相同的 `template.file_path`
- `region`: 与输入相同的地域
- `description`: 修复后模板说明
- `validated`: `true`
- `review_passed`: `true`
- `review_issues`: InfraGuard 与 `ros_validate_template` 发现和修复过的问题列表；如果没有问题，填空数组
- `selected_review_aspects`: 已选择的 aspect key、名称和选择原因
- `skipped_review_aspects`: 未选择的 aspect key、名称和跳过原因
- `resolved_infraguard_policies`: 最新可用 `infraguard_scan` 返回的 `expanded_policies`
- `infraguard_summary`: 最新可用 InfraGuard 扫描摘要，包含 `passed`、`blocking_findings`、`findings` 等关键信息
- `fix_summary`: 修复动作摘要
