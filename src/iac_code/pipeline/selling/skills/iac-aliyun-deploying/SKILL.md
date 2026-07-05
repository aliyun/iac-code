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
3. **均无**（工具参数无默认值且用户未指定）→ 请用户指定目标地域

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
- 否则，部署前必须校验模板文件。调用 `ros_validate_template` 校验，`template_url` 使用当前步骤 prompt 中已选定的具体模板文件路径；已有具体地域时传 `region_id`，否则使用工具默认地域。不要通过 `aliyun_api` 调用 ROS 模板校验或部署生命周期接口。校验失败时分析错误原因，查 GetResourceType Schema（如需），修复模板文件后重试（最多 5 轮）。模板文件会被后续步骤依赖，必须确保其内容正确后再继续。
- `ros_deploy` 的 `create` 失败后，如果需要修改模板，成本步骤的预览验证已失效；修复后必须重新调用 `ros_validate_template`，通过后再调用 `ros_deploy` 的 `continue_create`。只调整部署参数时，不需要为了参数变化补跑 `ros_validate_template`；最终参数由 `ros_deploy` 的部署调用校验。

## 可用性查询

快速创建路径已跳过例行可用性查询。其他情况下，当用户确认执行以下操作时，**必须先查询可用性**：

| 操作 | 查询范围 |
|------|----------|
| ros_deploy create | 全量查询所有库存相关 Parameters |
| ros_deploy continue_create | 查询失败资源相关的 Parameters |
| ros_deploy delete_and_create | 按替代创建参数全量查询库存相关 Parameters |

查询步骤：
1. 解析模板 Parameters，识别库存相关参数及对应产品
2. 调用各产品可用性 API（具体 API 见 [references/cloud-products/](references/cloud-products/) 各产品文件的「可用性查询」节）
3. 核对最终部署参数中的可用区和规格是否可用
4. 参数不可用时先报告冲突详情并尝试调整非用户指定参数；仍无法成功创建资源栈时，才可调整用户指定参数

无法找到公共可用区时，告知用户冲突详情，建议换规格系列或换地域。

## 部署参数装配

调用 `ros_deploy` 的 `create` 前按以下优先级确定 `parameters`：

1. `selected_plan.effective_deployment_parameters` 非空时，直接作为最终部署参数集。
2. 否则使用 `selected_plan.selected_candidate_result.cost.deployment_parameters`。
3. 仍缺少模板必填参数时，使用模板 Default 或上下文已有值补足；无法补足时返回 `status: failed` 或通过 rollback_request 回到 `confirm_and_select`。

装配参数时不得改写模板 `Default`，不得编造缺失的外部输入（LicenseKey、Token、证书、真实域名、已有资源 ID、VpcId、VSwitchId、SecurityGroupId、KeyPairName 等）。参数不可用或部署调用无法成功时，优先调整非用户指定参数；仍无法成功创建资源栈时，才可调整用户指定参数。部署步骤不计算费用。

## StackName

新建 Stack 时，一开始就确定唯一 `StackName`，并作为 `stack_name` 传给 `ros_deploy` 的 `create`。`StackName` 使用方案或服务简名作为前缀，并追加时间或 6 位小写字母/数字随机串后缀（如 `ai-app-20260623-a1b2c3`），避免重名。

- `ros_deploy` 的 `create` 必须传 `stack_name`，不要省略，不要使用容易重复的固定名称。
- `ros_deploy` 的 `continue_create` 面向已有失败 Stack 时，使用 `create` 失败结果中的 Stack 标识，不要生成新的 StackName。
- `ros_deploy` 的 `delete_and_create` 面向已有失败 Stack 时，`stack_id` 使用旧失败 Stack 标识；`stack_name` 使用替代创建目标的名称。

## 执行部署

- 使用 `ros_deploy` 工具执行 `create` / `continue_create` / `delete_and_create`，禁止用 Bash
- `ros_deploy` 的 `create` 会使用 `DisableRollback: true`
- `ros_deploy` 使用装配后的 `parameters` 字典；不要手动展开为 `Parameters.N.ParameterKey`

> **template_url 支持本地文件路径**：`ros_deploy` 中 `template_url` 可传本地文件路径（如 `/tmp/template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 错误处理

### 部署失败
分析错误原因：
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
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） |
| [references/ros-template.md](references/ros-template.md) | ROS 原生模板最佳实践：RunCommand、嵌套栈、条件部署 |
