---
name: iac-aliyun-deploying
description: 阿里云 ROS 模板部署技能，负责可用性查询、执行部署/更新/删除/继续创建等 Stack 操作
when_to_use: 当用户确认部署 ROS 模板、更新 Stack、删除 Stack 时
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
---

# 阿里云 ROS 部署技能

负责将 ROS 模板部署到阿里云，包括可用性查询和 Stack 生命周期管理。

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
- 仅当本技能被用户直接触发，或删除/更新等高风险操作没有上层确认时，才需要先询问用户确认；删除/更新操作使用 ⚠️ 警告措辞。
- `status: cancelled` 只表示用户明确取消部署，不得用 status: cancelled 表示等待用户确认。

## 模板校验

部署前必须校验模板文件。调用 aliyun_api(product="ros", action="ValidateTemplate", params={"TemplateURL": <模板文件路径>}) 校验。校验失败时分析错误原因，查 GetResourceType Schema（如需），修复模板文件后重试（最多 5 轮）。模板文件会被后续步骤依赖，必须确保其内容正确后再继续。

## 可用性查询

当用户确认执行以下操作时，**必须先查询可用性**：

| 操作 | 查询范围 |
|------|----------|
| CreateStack | 全量查询所有库存相关 Parameters |
| ContinueCreateStack | 查询失败资源相关的 Parameters |
| UpdateStack | 查询变更涉及的 Parameters |

查询步骤：
1. 解析模板 Parameters，识别库存相关参数及对应产品
2. 调用各产品可用性 API（具体 API 见 [references/cloud-products/](references/cloud-products/) 各产品文件的「可用性查询」节）
3. 找出公共可用区（所有资源都有库存的可用区）
4. 按 cloud-products 中的推荐规格优先匹配，不可用时选最接近的替代
5. 得到选定参数；若上层 pipeline 已确认部署，展示选定结果后继续执行，不要再次请求用户确认。

无法找到公共可用区时，告知用户冲突详情，建议换规格系列或换地域。

## 执行部署

- 使用 ros_stack 工具执行 CreateStack/UpdateStack/ContinueCreateStack/DeleteStack，禁止用 Bash
- CreateStack 必须传 `DisableRollback: true`

> **TemplateURL 支持本地文件路径**：ros_stack 中 TemplateURL 可传本地文件路径（如 `/tmp/template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 错误处理

### 部署失败
分析错误原因：
- 权限/配额 → 告知用户处理
- 模板/参数 → 修复后 ContinueCreateStack（不重新 CreateStack）

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
