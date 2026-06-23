# 20260623 fix-skill cherry-pick

## 背景

将 `/Users/ehzyo/open_repo/iac-code3/.worktrees/fix-skill` 最近 5 个提交合回主工作区 `/Users/ehzyo/open_repo/iac-code3` 的 `fix_pipeline` 分支。

## 合入提交

- `a41e479 fix: refine aliyun selling pipeline skills`
- `df374c2 feat: add pipeline surface prompt overrides`
- `18699f8 fix: enforce ROS stack completion guard`
- `7d17bd5 fix: require unique ROS stack names`
- `0174c39 fix: restrict ROS TemplateBody in pipeline mode`

## 主要内容

- 对齐 selling pipeline 下 3 个阿里云技能的提示和评测，补充参数推荐、PreviewStack、参数透传、部署守卫等行为说明。
- 为 `confirm_and_select` 增加 surface override 支持，使 REPL 和 A2A 可以使用不同 prompt；A2A 使用更薄的选择协议 prompt。
- 增加部署完成守卫，要求部署成功结论必须和 `ros_stack` 成功工具结果匹配，避免无 stack_id 时直接提交成功结论。
- 要求部署 StackName 带随机后缀，并在 cost 阶段的 PreviewStack 明确传入 StackName。
- 在 pipeline 工具上下文中限制 ROS 模板调用使用 `TemplateURL`，但非 pipeline 模式仍兼容传统 `TemplateBody`。

## 冲突处理

- `pipeline.yaml` 中保留 `parameter_overrides` 和 `completion_guards`，没有恢复已经移除的静态 rollback 规则。
- `pipeline_runner.py`、`deploying.py`、`test_terminal_ui_contract.py`、`test_step_executor.py` 合并了目标分支已有的 cleanup、用户输入、skill root 读权限等逻辑。
- `session_index.py` 保留本地 `CLEANUP_PROMPT_METADATA_TYPE` 常量，避免 normal mode 导入 pipeline engine。
- 本次 cherry-pick 没有遇到国际化文件冲突；后续按验证结果执行 `make translate`，将新增 msgid 同步到各语言 `messages.po`，没有手工删除既有词条。

## 验证

按要求执行：

- `make lint`
- `make format`
- `make test`
