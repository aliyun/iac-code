---
name: iac-aliyun-review
description: 模板安全、最佳实践和合规审查，发现问题必须回溯修复
conclusion_schema:
  type: object
  required: [passed, issues, summary]
  additionalProperties: false
  properties:
    passed:
      type: boolean
      description: 审查是否通过（无 high 级问题）
    issues:
      type: array
      items:
        type: object
        required: [severity, category, description, suggestion]
        properties:
          severity:
            type: string
            enum: [high, medium, low]
          category:
            type: string
            enum: [security, ha, practice, correctness]
          description:
            type: string
          suggestion:
            type: string
    summary:
      type: string
      description: 审查摘要
---

# 模板审查

对生成的 IaC 模板进行安全和最佳实践审查。**发现问题时必须通过 rollback_request 回溯修复，不能只报告问题。**

## 审查维度

1. **安全性**
   - 安全组是否限制了不必要的入站端口（0.0.0.0/0 开放非 80/443 端口 = 高危）
   - 是否启用了加密（磁盘、传输、存储）
   - 是否暴露了不必要的公网 IP
   - RAM 权限是否最小化

2. **高可用**
   - 关键资源是否跨可用区
   - 是否配置了健康检查
   - 数据库是否启用了备份

3. **最佳实践**
   - 资源命名是否规范（使用 Stack 名称前缀）
   - 是否添加了标签（至少 Environment、Project）
   - Parameters 是否有合理 AllowedValues 约束
   - Outputs 中 Fn::GetAtt 引用的属性是否存在

4. **模板正确性**
   - 资源依赖是否正确（DependsOn）
   - 引用的可用区是否存在于目标地域
   - 模板语法是否合法（可通过 `aliyun_api` 调用 ROS `ValidateTemplate` 校验）

## 行为规则

- **通过审查**（无 high 级问题）：调用 `complete_step`，`conclusion.passed = true`
- **未通过审查**（发现 high 级问题）：**必须**在 `complete_step` 中填写 `rollback_request`：
  - 所有模板问题（语法/引用/安全组/规格配置） → 回溯到 `template_generating`
- **不要**只报告问题而不回溯修复

## 可选工具使用

- 可调用 `aliyun_api` 的 ROS `ValidateTemplate` 接口校验模板语法
- 不需要调用其他工具

## 输出
调用 `complete_step` 提交结论。字段定义见 tool schema。

如果 `passed` 为 `false`（存在 high 级问题），**同时必须**填写 `rollback_request`，target_step 为 `template_generating`。
