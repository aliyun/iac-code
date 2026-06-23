# Pipeline Schema Reference

This document describes review-relevant `pipeline.yaml` fields that are not obvious from the high-level pipeline docs. Examples are taken from `src/iac_code/pipeline/selling/pipeline.yaml`.

## Step Fields

### `completion_guards`

`completion_guards` declares checks that must pass before a step can be accepted as complete. Guards are evaluated against the current user input, the proposed step conclusion, and optional tool results.

Common keys:

- `require_tool`: the named tool must have been called before completion.
- `when_user_message_matches_any`: regex list that activates the guard when any pattern matches the user message.
- `unless_user_message_matches_any`: regex list that suppresses the guard.
- `when_conclusion_field_equals`: mapping of conclusion field names to required values.
- `required_conclusion_field`: a single conclusion field that must be present and truthy.
- `required_conclusion_any_of`: at least one listed conclusion field must be present and truthy.
- `require_tool_result`: require a matching tool result, with optional `tool`, `action_in`, `is_success`, `status_in`, and `match_conclusion_field`.
- `copy_tool_result_to_conclusion`: mapping used to copy fields from the required tool result into the conclusion.
- `message`: user-facing or diagnostic explanation when the guard blocks completion.

Example:

```yaml
completion_guards:
  - require_tool: ask_user_question
    when_user_message_matches_any:
      - "(项目|需求|应用|服务|网站|小程序|后端).*(上线|部署)"
    unless_user_message_matches_any:
      - "(ECS|RDS|OSS|VPC|SLB|ALB|NAT|Redis|Kafka|CDN|K8s|Kubernetes|阿里云)"
    required_conclusion_any_of: [clarification_choice, clarification_text]
    copy_tool_result_to_conclusion:
      selected_id: clarification_choice
      free_text: clarification_text
    message: "这个输入仍缺少明确的云资源、部署目标或运维约束，需要先向用户澄清。"
```

Deployment completion can also be gated on a cloud tool result:

```yaml
completion_guards:
  - when_conclusion_field_equals:
      status: success
    required_conclusion_field: stack_id
    require_tool_result:
      tool: ros_stack
      action_in: [CreateStack, ContinueCreateStack]
      is_success: true
      status_in: [CREATE_COMPLETE]
      match_conclusion_field: stack_id
    message: "部署成功必须等待 ros_stack CreateStack 返回 CREATE_COMPLETE。"
```

### `surface_overrides`

`surface_overrides` customizes selected step fields for a specific runtime surface. The current supported override keys are:

- `prompt`: alternate prompt file.
- `inject_tools`: replacement injected tool list.

Example:

```yaml
surface_overrides:
  a2a:
    prompt: prompts/confirm_and_select.a2a.md
    inject_tools: []
```

### `parameter_overrides`

`parameter_overrides` is a conclusion field used by candidate selection to carry deployment parameter overrides into the deploying step. It is a mapping from ROS parameter names to user-provided values.

Example from the candidate selection conclusion schema:

```yaml
conclusion_schema:
  type: object
  properties:
    parameter_overrides:
      type: object
      description: 用户选择方案时传入的部署参数覆盖字典；键为 ROS Parameters 名称，值为用户指定的部署参数值；首次展示方案时可省略
```

### `a2a_artifacts`

`a2a_artifacts` extracts files from a completed step conclusion and publishes them as A2A artifacts.

Keys:

- `path`: dotted path to a filename or artifact path.
- `content`: dotted path to artifact content.
- `media_type`: explicit media type or `auto`.

Example:

```yaml
a2a_artifacts:
  - path: conclusion.file_path
    content: conclusion.template
    media_type: auto
```

### `exit_condition`

`exit_condition` allows a step to end the pipeline early when a conclusion field equals a configured value. It is usually used for intent parsing.

Example:

```yaml
exit_condition:
  field: is_infra_intent
  value: false
```

### `inject_tools`

`inject_tools` adds pipeline-specific tools to a step in addition to the selected base tool set. The `complete_step` tool is injected automatically and should not be listed.

Example:

```yaml
inject_tools: [ask_user_question]
```

### `ui_mode`

`ui_mode` tells UI surfaces to use a specialized renderer or interaction mode for a step.

Example:

```yaml
ui_mode: candidate_selection
```

### `conclusion_schema`

`conclusion_schema` is a JSON Schema object that validates the `conclusion` passed to `complete_step`. A step-level schema overrides any schema loaded from the step skill.

Example:

```yaml
conclusion_schema:
  type: object
  required: [user_prompt, options]
  additionalProperties: false
  properties:
    user_prompt:
      type: string
    options:
      type: array
      minItems: 1
      items:
        type: object
        required: [name, summary, candidate_index]
        additionalProperties: false
        properties:
          name:
            type: string
          summary:
            type: string
          candidate_index:
            type: integer
```

### `interrupt_judge_failure`

`interrupt_judge_failure` controls what happens when the interrupt judge fails while evaluating whether user input should interrupt or roll back a running step.

Supported values are implementation-defined; the selling pipeline uses:

- `pause`: pause instead of silently continuing when the judge fails.
- default behavior when omitted: continue.

Example:

```yaml
interrupt_judge_failure: pause
```

### `hooks_file`

`hooks_file` points to a Python hook module relative to the pipeline directory. Hook modules can define lifecycle callbacks such as resource observation or rollback cleanup handling for a step.

Example:

```yaml
hooks_file: hooks/deploying.py
```

### `enabled_when`

`enabled_when` references a pipeline feature flag. If the flag is false, the step is skipped.

Example:

```yaml
feature_flags:
  enable_reviewing:
    default: false
    env: IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING

steps:
  - id: reviewing
    enabled_when: enable_reviewing
```

## Related Include/Exclude Configs

Several fields use the same include/exclude shape:

```yaml
tools:
  include: [read_memory]
  exclude: []
```

An empty `include` means all base entries for that category. A non-empty `include` means only the listed entries. `exclude` removes entries from the result.
