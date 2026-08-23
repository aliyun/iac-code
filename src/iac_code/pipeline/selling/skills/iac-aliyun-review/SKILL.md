---
name: iac-aliyun-review
description: 使用 InfraGuard 审查 ROS 模板；有发现时直接修复并验证，初始扫描干净时直接输出模板
when_to_use: 当需要对售卖 pipeline 中生成的阿里云 ROS 模板进行合规、安全和最佳实践修复时
user_invocable: false
conclusion_schema:
  type: object
  required: [template, template_sha256, file_path, region, description, validated, review_passed, review_issues, selected_review_aspects, skipped_review_aspects, resolved_infraguard_policies, infraguard_summary, fix_summary]
  additionalProperties: false
  properties:
    template:
      type: string
      description: 与写入文件逐字一致的裸 YAML 字符串；禁止 markdown 代码围栏包裹，禁止模板前后的说明性段落或注释
    template_sha256:
      type: string
    file_path:
      type: string
    region:
      type: string
    description:
      type: string
    validated:
      const: true
    review_passed:
      const: true
    review_issues:
      type: array
    selected_review_aspects:
      type: array
    skipped_review_aspects:
      type: array
    resolved_infraguard_policies:
      type: array
    infraguard_summary:
      type: object
    fix_summary:
      type: string
---

# ROS 模板审查与修复

本技能用于售卖 pipeline 的 `reviewing` 步骤。它不是单独生成审查报告，而是用 InfraGuard 扫描 ROS 模板：如果初始扫描已经通过且不需要修改模板，直接用该扫描结果完成；如果发现需要修复的问题，则直接修复原模板文件，通过 `ros_validate_template`，再执行最终 InfraGuard 扫描，最后用修复后的模板覆盖当前 `template` 结论。

## 输入与边界

- 从上下文读取 `intent`、`candidate`、`template.file_path`、`template.region`、`template.description` 和 `template.template`。
- 必须先用 `read_file` 读取 `template.file_path` 指向的原模板文件，以文件内容为准。
- InfraGuard 安装、可执行文件查找和策略更新由 pipeline prerequisites 管理；不要执行 InfraGuard 的 policy update 命令。
- 不要在技能中维护或复制 InfraGuard 官方策略目录、策略包或 Rego package bodies；策略来源以 InfraGuard CLI 当前策略库为准。
- 不要默认使用 rollback_request 回到 `template_generating`；本步骤负责直接修复当前模板。
- 不要把问题只放进单独的 `review` 字段；本步骤的结论字段是 `template`，必须输出修复后的模板内容和同一个文件路径。

## Aspect 选择

`config.infraguard.aspects` 定义了可选审查维度和每个维度对应的 InfraGuard policies。你只能选择这些已配置的 aspect key，不要自己编写或猜测 policy id。

根据 `intent`、`candidate` 和模板资源判断适用维度：

- 安全性和通用最佳实践通常适用于所有可部署模板。
- 高可用只在用户或候选方案要求生产级、多可用区、容灾或 SLA 时选择；低成本单机方案通常跳过。
- 弹性能力只在用户提到突发流量、秒杀、自动扩缩容，或候选方案包含弹性伸缩/动态容量设计时选择。
- 合规性只在用户提出行业合规、等保、审计、企业治理或监管要求时选择。
- 网络架构在模板包含 VPC、VSwitch、SLB/ALB、EIP、公网访问或网络隔离要求时选择。
- 模板被本步骤修改后必须检查正确性：资源依赖（DependsOn）、引用的可用区和地域、ROS 模板语法与 schema 都需要通过 `ros_validate_template` 验证。

记录选择结果：

- `selected_aspects`: 传给 `infraguard_scan` 的 aspect key 数组。
- `selected_review_aspects`: conclusion 中记录每个已选 aspect 的 key、名称和选择原因。
- `skipped_review_aspects`: conclusion 中记录每个未选 aspect 的 key、名称和跳过原因。
- `resolved_infraguard_policies`: conclusion 中记录最新可用扫描工具返回的 `expanded_policies`。

## InfraGuard 扫描

调用 `infraguard_scan`，参数来自当前 step config 中的 `config.infraguard`：

- `file_path`: `template.file_path`
- `mode`: `mode`
- `selected_aspects`: 基于 `intent`、`candidate` 和模板资源选择出的 aspect key 数组
- `aspect_policy_map`: `aspects`
- `ignore_waivers`: `ignore_waivers`
- `blocking_severities`: `blocking_severities`

解释 findings 时，优先提取资源名、属性路径、severity、规则说明和修复建议。`high`、`critical` 或出现在 `blocking_severities` 中的 finding 都必须修复，不能只记录。

如果 `infraguard_scan` 返回工具错误或错误 payload（例如 `command_not_found`、`timeout`、`malformed_json`、`unexpected_exit_code`、`unknown_policy_aspect`），本步骤不能继续走成功收口。必须记录可处理的错误上下文：错误类型、`command`、`stderr` 摘要、`file_path`、`selected_aspects`，以及已经展开出的 policies（如果工具返回了）。不要在 skill 内执行策略更新命令，不要调用 `complete_step` 输出 `validated=true` 或 `review_passed=true`。

## 直接修复流程

1. 读取 `template.file_path`。
2. 根据 `intent`、`candidate` 和模板资源选择 `selected_aspects`，只选择 step config 中存在的 aspect key。
3. 使用 step config 值调用 `infraguard_scan`，传入 `selected_aspects`、`aspect_policy_map=config.infraguard.aspects`，并设置 `include_file_content=true`。
4. 如果初始 `infraguard_scan` 返回 `passed=true`、`blocking_findings=0`，且没有需要修复的 finding，不要调用 `ros_validate_template`，不要再次调用 `infraguard_scan`；直接使用这次扫描返回的 `file_content` 和 `file_sha256` 完成。
5. 如果 findings 需要修改模板，根据 findings 修改原模板文件；使用 `edit_file` 或 `write_file` 写回同一个 `template.file_path`。
6. 只要本步骤修改过 `template.file_path`，就必须调用 `ros_validate_template(template_url=template.file_path)`。
7. 如果 `ros_validate_template` 失败，分析错误，必要时使用 `aliyun_doc_search` 查询 ROS schema/文档，然后继续修复同一个原模板文件。
8. 修复轮数不得超过 step config 的 `max_fix_rounds`。如果到达上限仍无法通过，不要伪造通过结论。
9. `ros_validate_template` 成功后，最终再次运行 `infraguard_scan`，仍使用相同 `selected_aspects`、step config 和同一个 `template.file_path`，并设置 `include_file_content=true`。
10. 只有最新可用扫描 `passed=true` 且 `blocking_findings=0` 时，才能调用 `complete_step`。如果本步骤没有改动模板，初始扫描就是最新可用扫描；如果改动过模板，最终扫描才是最新可用扫描。

> **template_url 支持本地文件路径**：`ros_validate_template` 的 `template_url` 可传 `template.file_path` 本地路径，工具会自动读取文件内容。不要把大模板内容直接塞进 API 参数。

## 修复原则

- 保持模板是 ROS YAML/JSON，不引入 Terraform 或其他 IaC 格式。
- 修复依据只能来自 InfraGuard findings、`ros_validate_template` 错误或用户明确约束；不要在技能内硬编码额外的安全、合规、架构规则。
- 优先做最小修复：只改动能够消除对应 finding 或校验错误的资源、属性和引用，保留用户意图与候选方案的设计边界。
- 不要改变用户明确要求的资源生命周期；已有资源仍应建模为 Parameters 或外部引用，不要擅自在 `Resources` 中创建。
- 修改库存相关属性时，遵循模板生成技能的参数化约束，不硬编码实例规格、可用区等库存值。
- 如果某个 finding 无法从模板静态修复，记录到 `review_issues`，但只有 blocking findings 为 0 且 `ros_validate_template` 通过时才能完成。

## 完成条件

调用 `complete_step` 前必须同时满足：

- 已经对同一个 `template.file_path` 执行 `infraguard_scan`。
- 最新可用 `infraguard_scan` 结果中 `passed=true` 且 `blocking_findings=0`。
- 最新可用 `infraguard_scan` 必须使用 `include_file_content=true`，并返回 `file_content`、`file_sha256`、`selected_aspects` 和 `expanded_policies`。
- 如果本步骤对同一个 `template.file_path` 执行过 `edit_file` 或 `write_file`，必须先对同一个 `template.file_path` 调用 `ros_validate_template(template_url=template.file_path)` 并成功，然后执行最终 `infraguard_scan`。
- 如果本步骤没有修改模板，初始 clean 的 `infraguard_scan` 可直接作为最新可用扫描，不要额外调用 `ros_validate_template` 或第二次 `infraguard_scan`。
- `complete_step.conclusion.template` 必须精确等于最新可用 `infraguard_scan.file_content`。
- `complete_step.conclusion.template_sha256` 必须精确等于最新可用 `infraguard_scan.file_sha256`，也必须等于 `conclusion.template` 的 sha256。
- 不要凭内存、摘要、旧版本 `read_file` 或旧扫描结果填写 `conclusion.template`。

## 输出

`complete_step` 的 `conclusion` 必须符合 schema：

- `template`: 最新模板内容，必须原样使用最新可用 `infraguard_scan.file_content` 返回的完整内容。
- `template_sha256`: 最新可用 `infraguard_scan.file_sha256`，必须是 `template` 字符串的 sha256。
- `file_path`: 原 `template.file_path`，不要换路径。
- `region`: 原地域。
- `description`: 模板说明，可补充修复摘要。
- `validated`: 固定为 `true`。
- `review_passed`: 固定为 `true`。
- `review_issues`: 数组，记录 InfraGuard findings、`ros_validate_template` 错误和对应修复；无问题则为空数组。
- `selected_review_aspects`: 数组，记录已选择的 aspect key、名称和选择原因。
- `skipped_review_aspects`: 数组，记录未选择的 aspect key、名称和跳过原因。
- `resolved_infraguard_policies`: 数组，记录最新可用扫描使用的 InfraGuard policy id，来自 `infraguard_scan.expanded_policies`。
- `infraguard_summary`: 最新可用扫描摘要，至少保留 `passed`、`blocking_findings`、`summary` 或关键 findings 数量。
- `fix_summary`: 简短说明修复了什么；若无需修复，说明初始 InfraGuard 扫描通过且没有修改模板。
