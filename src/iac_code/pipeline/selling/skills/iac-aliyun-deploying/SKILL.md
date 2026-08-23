---
name: iac-aliyun-deploying
description: 阿里云 ROS 模板部署技能，负责可用性查询、执行部署与失败恢复
when_to_use: 当用户确认部署 ROS 模板时
user_invocable: false
conclusion_schema:
  type: object
  required: [status]
  additionalProperties: false
  properties:
    stack_id:
      type: string
      description: ROS Stack ID（部署成功时必填）
    status:
      type: string
      enum: [success, failed, cancelled]
      description: 部署状态
    resources_created:
      type: array
      items:
        type: string
    outputs:
      type: object
    deployment_recovery:
      type: object
      description: 部署过程中出现过 ros_deploy 创建失败并恢复时必填；记录累计重试次数、每次失败原因与恢复路径
      required: [retry_count, failed_attempts, recovery_path]
      additionalProperties: false
      properties:
        retry_count:
          type: integer
          minimum: 1
          description: 本步骤 ros_deploy 创建类动作累计失败的次数
        failed_attempts:
          type: array
          minItems: 1
          description: 每次失败的创建类动作，按发生顺序排列
          items:
            type: object
            required: [action, reason]
            additionalProperties: false
            properties:
              action:
                type: string
                enum: [create, continue_create, delete_and_create]
              stack_id:
                type: string
              status:
                type: string
                description: 该次失败的 Stack 状态，如 CREATE_FAILED
              reason:
                type: string
                description: 该次失败的具体原因
        recovery_path:
          type: string
          description: CREATE_FAILED→修复→成功 的恢复路径，说明每轮采取的修复动作
    error:
      type: string
      description: 失败原因（status 为 failed 时必填）
  allOf:
    - if:
        properties:
          status:
            const: success
        required: [status]
      then:
        required: [stack_id]
    - if:
        properties:
          status:
            const: failed
        required: [status]
      then:
        required: [error]
---

# 阿里云 ROS 部署技能

负责将 ROS 模板部署到阿里云，包括可用性查询和部署失败恢复。

## 地域

所有 API 调用都需要地域，按以下优先级确定：
1. **用户指定**（如"在北京创建"）→ 使用用户指定的地域
2. **工具默认地域**（用户未指定时）→ aliyun_api 工具的 region_id 参数描述中会显示默认地域（如 `Defaults to 'cn-hangzhou'`），使用该默认值并告知用户
3. **均无**（工具参数无默认值且用户未指定）→ 不发起澄清问题；返回失败并说明缺少目标地域

确定后，所有 API 调用统一使用该地域。

## 部署前确认

写操作必须有用户确认，但确认来源可以是上层 pipeline：
- 当 pipeline prompt 明确说明用户已确认选择/部署时，表示 pipeline 已完成部署确认，不要再次请求用户确认。
- 在已确认的 pipeline 部署步骤中，可展示将使用的 VPC、可用区、网段、Stack 名等参数摘要，但展示后必须继续执行部署，不要询问“是否确认部署”或“是否确认部署参数”。
- 仅当本技能被用户直接触发，或更新等高风险操作没有上层确认时，才需要先询问用户确认；更新操作使用 ⚠️ 警告措辞。
- 本步骤通过 `ros_deploy` 恢复失败部署。`delete_and_create` 只允许删除本步骤创建的失败 Stack；非本步骤创建的 Stack（例如通过 ListStacks 查到的 Stack）不得删除。如用户请求删除其他 Stack，必须另走明确“确认删除”的删除流程，不得在本步骤执行。
- `status: cancelled` 只表示用户明确取消部署，不得用 status: cancelled 表示等待用户确认。

## 快速创建与模板校验

- `selected_plan.preview_ready_for_create` 为 `true` 时，表示成本步骤已对同一模板路径完成预览验证，且没有完整部署参数缺口；部署时直接调用 `ros_deploy` 的 `create`，跳过例行 `ros_validate_template`，并跳过例行可用性查询。用户覆盖后的最终部署参数由 `ros_deploy` 的部署调用做最终校验。
- 否则，部署前必须校验模板文件。调用 `ros_validate_template` 校验，`template_url` 使用 `selected_plan.template_url`，也就是当前步骤 prompt 中已选定的具体模板文件路径；已有具体地域时传 `region_id`，否则使用工具默认地域。不要通过 `aliyun_api` 调用 ROS 模板校验或部署生命周期接口。校验失败时分析错误原因，查 GetResourceType Schema（如需），只能使用 `edit_file` 就地修复 `selected_plan.template_url` 指向的原模板文件后重试（最多 5 轮）；不得写入新的模板文件，不得改用新的模板路径。模板文件会被后续步骤依赖，必须确保其内容正确后再继续。
- `ros_deploy` 的 `create` 失败后，如果需要修改模板，成本步骤的预览验证已失效；修复后必须重新调用 `ros_validate_template`，通过后再调用 `ros_deploy` 的 `continue_create`。只调整部署参数时，不需要为了参数变化补跑 `ros_validate_template`；最终参数由 `ros_deploy` 的部署调用校验。
- `ros_deploy` 的 `create` / `continue_create` / `delete_and_create` 已经发起 ROS 操作但工具调用超时或中断时，不要再次调用创建类动作。使用同一 `stack_id` 调用 `ros_deploy` 的 `wait`，它只轮询已有 Stack 的创建进度，不会调用 CreateStack 或 ContinueCreateStack。

## 部署前参数补全

`selected_plan.parameter_overrides` 是用户在选择步骤给出的最新参数选择，首次创建时优先级最高，不得主动替换。只有真实的只读 API 或 `ros_deploy` 结果证明该值不可用，并且所有非用户指定参数的调整方案都已耗尽后，才可修改对应的用户参数；修改时必须向用户说明失败证据、原值、新值和调整原因，不得仅凭推荐、默认值或经验主动改写。

快速创建标记不为 true，或 `selected_plan.selected_candidate_result.cost.missing_deployment_parameters` 非空时，不要把成本阶段的参数缺口当成最终结论。部署阶段可以继续使用 `ros_get_template_parameter_constraints` 补全参数，但不要调用询价工具，也不要向用户发起澄清问题。

参数补全流程：
1. 先从 `selected_plan.effective_deployment_parameters`、`selected_plan.selected_candidate_result.cost.deployment_parameters`、用户 `parameter_overrides`、模板 Default 和上下文已有值合并当前参数。
2. 仍缺少模板必填参数时，调用 `ros_get_template_parameter_constraints`，传当前 `parameters` 字典继续求解可用候选。
3. 对可推断配置（名称、CIDR、布尔值、小整数、非敏感字符串、模板安全默认值）直接给出合规值；对普通密码（ECS/RDS/Redis/RocketMQ/WordPress 等密码，或参数名、`NoEcho`、AssociationProperty、描述/约束表明是密码）生成合规随机值，必须满足长度、复杂度、`AllowedPattern`、`ConstraintDescription`。同一个真实值必须贯穿参数补全、`parameters`、结构化结论和部署，不得替换为 `***`、`[REDACTED]` 或 `<redacted>`；服务端日志由运行时单独脱敏。
4. 对库存相关参数只在工具/API 返回的合法候选内筛选或排序，不得编造库存值；对 LicenseKey、Token、证书、真实域名、已有资源 ID、VpcId、VSwitchId、SecurityGroupId、KeyPairName 等外部或账号特定输入，不得编造。
5. 补齐后的参数不再调用预览工具；直接进入 `ros_deploy` 创建类动作，由部署调用做最终参数校验。部署错误指向参数时按上述优先级恢复；错误指向模板时按模板校验/修复流程处理。

不得仅因部署参数缺失返回 `status: failed`。只有在已经先尽量补齐或生成参数、调用可用工具仍无法形成合法完整参数集，且剩余缺口属于不得编造的外部输入时，才允许失败或回滚；失败原因必须列出剩余缺口和为什么不能自动补齐。

## 可用性查询

快速创建路径已跳过例行可用性查询。其他情况下，当用户确认执行以下操作时，**必须先查询可用性**：

| 操作 | 查询范围 |
|------|----------|
| ros_deploy create | 全量查询所有库存相关 Parameters |
| ros_deploy continue_create | 查询失败资源相关的 Parameters |
| ros_deploy delete_and_create | 按替代创建参数全量查询库存相关 Parameters |
| ros_deploy wait | 不查询库存；仅等待已发起创建的 Stack 达到终态 |

查询步骤：
1. 解析模板 Parameters，识别库存相关参数及对应产品
2. 调用各产品可用性 API（具体 API 见 [references/cloud-products/](references/cloud-products/) 各产品文件的「可用性查询」节）
3. 核对最终部署参数中的可用区和规格是否可用
4. 参数不可用时按「部署前参数补全」中的优先级恢复

无法找到公共可用区时，告知用户冲突详情，建议换规格系列或换地域。

## 部署参数装配

调用 `ros_deploy` 的 `create` 前按以下优先级装配 `parameters`：

1. `selected_plan.effective_deployment_parameters` 非空时，作为当前参数基础；不得因它非空就视为完整。
2. 否则使用 `selected_plan.selected_candidate_result.cost.deployment_parameters` 作为当前参数基础。
3. `selected_plan.selected_candidate_result.cost.missing_deployment_parameters` 非空，或仍缺少模板必填参数时，按「部署前参数补全」先尽量补齐或生成参数，再交由 `ros_deploy` 做最终参数校验。

装配参数时不得改写模板 `Default`，不得编造缺失的外部输入（LicenseKey、Token、证书、真实域名、已有资源 ID、VpcId、VSwitchId、SecurityGroupId、KeyPairName 等）。部署步骤不计算费用。

## StackName

新建 Stack 时，一开始就确定唯一 `StackName`，并作为 `stack_name` 传给 `ros_deploy` 的 `create`。用户指定名称时将其作为基础名，否则使用方案或服务简名；两者都追加时间或 6 位小写字母/数字随机串后缀（如 `ai-app-20260623-a1b2c3`），避免重名。

- `ros_deploy` 的 `create` 必须传 `stack_name`，不要省略，不要使用容易重复的固定名称。
- `ros_deploy` 的 `continue_create` 面向已有失败 Stack 时，使用 `create` 失败结果中的 Stack 标识，不要生成新的 StackName。
- `ros_deploy` 的 `delete_and_create` 面向已有失败 Stack 时，`stack_id` 使用旧失败 Stack 标识；`stack_name` 使用替代创建目标的名称。
- `ros_deploy` 的 `wait` 面向已有创建中 Stack 时，只传 `stack_id` 和 `region_id`；不要传 `template_url`、`parameters`，不要生成新的 StackName。

## 执行部署

- 使用 `ros_deploy` 工具执行 `create` / `continue_create` / `delete_and_create` / `wait`，禁止用 Bash
- `ros_deploy` 的 `create` 会使用 `DisableRollback: true`
- `ros_deploy` 的 `wait` 只等待已有 Stack 创建完成，不发起创建、继续创建、删除或更新
- `ros_deploy` 的创建类动作使用装配后的 `parameters` 字典；不要手动展开为 `Parameters.N.ParameterKey`
- `ros_deploy` 成功结果包含 `outputs` 时，将其原样写入 `complete_step.conclusion.outputs`；不得使用模板表达式、占位符或推断值代替真实 Stack Outputs

> **template_url 支持本地文件路径**：`ros_deploy` 的创建类动作中，`template_url` 可传当前工作目录内的本地文件路径（如 `./template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 部署恢复记录

`ros_deploy` 的创建类动作（`create` / `continue_create` / `delete_and_create`）失败过、之后才成功时，最终结论不能只写 `status: success`。必须在 `complete_step.conclusion.deployment_recovery` 回填：

- `retry_count`：本步骤创建类动作累计失败的次数，与真实失败次数一致。
- `failed_attempts`：按发生顺序逐条记录每次失败的 `action`、`stack_id`、`status`（如 `CREATE_FAILED`）和 `reason`；`reason` 写该次失败的具体原因，不得留空或写成“部署失败”这类无信息描述。
- `recovery_path`：说明 CREATE_FAILED→修复→成功 的恢复路径，包含每轮采取的修复动作（如就地 `edit_file` 修模板、重新 `ros_validate_template`、改用 `continue_create` 或 `delete_and_create`）。

一次就成功、没有失败尝试时不需要 `deployment_recovery`。

## 错误处理

### 部署失败
分析错误原因：
- 工具调用超时但已有 `stack_id`，且 Stack 仍在创建 → 调用 `ros_deploy` 的 `wait`
- 权限/配额 → 告知用户处理
- 模板/参数 → 修复后调用 `ros_deploy` 的 `continue_create`
- `continue_create` 返回 `ContinueCreateStackValidationFailed` → 告知用户需要重建本步骤创建的失败 Stack，再调用 `ros_deploy` 的 `delete_and_create`

### 删除并重建
仅在 `continue_create` 返回 `ContinueCreateStackValidationFailed` 后使用 `delete_and_create`。调用时：
- `stack_id` 指向本步骤创建的旧失败 Stack，不得使用通过查询发现的其他 Stack
- `stack_name`、`template_url`、`parameters`、`region_id` 使用替代创建目标
- 工具会先确认替代创建参数和模板可用，再删除旧失败 Stack 后创建新的 Stack
- 成功后最终结果使用新 Stack 的 `stack_id`，不要把旧 `stack_id` 当成部署成功结果

## 资源和文档搜索

- 不确定的 ROS 资源属性或 Schema → aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})
- 不熟悉的资源类型/属性 → aliyun_doc_search（ROS 传 category_id=28850）
- 想要了解部署方案、云产品相关知识 → aliyun_doc_search
- 摘要不够 → web_fetch 获取完整文档

## aliyun_api 参数约定

**以下规则仅适用于 RPC 风格 API**（`style` 未传或传 `"RPC"`；ROA 风格用 JSON body/query，不受此约束）。

调用 RPC API 时，**array、object 类参数需平铺为带数字下标的键**，工具不会自动展开。规则：

- 下标从 `1` 起，依次递增
- `array[string]` → `<Name>.<N>`
- `array[object]` → `<Name>.<N>.<SubKey>`
- 嵌套列表按同样规则继续展开
- `object` → `<Name>.<SubKey>`

## 参考文件

| 文件 | 内容 |
|------|------|
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 |
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md、ga.md） |
| [references/ros-template.md](references/ros-template.md) | ROS 原生模板最佳实践：RunCommand、嵌套栈、条件部署 |
