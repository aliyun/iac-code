---
name: iac-aliyun-cost
description: 使用专用 ROS 模板询价工具预估 ROS 模板的月度部署费用，支持按需修复和校验模板问题
when_to_use: 当需要对阿里云 ROS 模板进行费用预估时
user_invocable: false
conclusion_schema:
  type: object
  required:
    - monthly_estimate
    - currency
    - resources
    - template_fixed
    - deployment_parameters
    - hard_constraint_checks
    - preview_validation
    - pricing_calibers
  additionalProperties: false
  properties:
    monthly_estimate:
      type: string
      description: 月度费用估算；询价同时返回 OriginalAmount 与 TradeAmount 时，必须同时包含列表价和合同优惠后价格（如 ¥96.80/月（列表价，合同优惠后约¥13.76/月））；询价失败时填 "询价失败"
    pricing_calibers:
      type: object
      required: [planning_estimate, list_price, calibers_aligned]
      additionalProperties: false
      description: 规划粗估与最终 ROS 询价的口径对照；complete_step 由代码校验对照完整性、偏差解释和优惠来源
      properties:
        planning_estimate:
          type: string
          description: 原样复制 candidate.monthly_estimate；架构规划未给出粗估时填 "未提供"
        list_price:
          type: string
          description: 本次询价的月度列表价（OriginalAmount 汇总，含金额与周期，如 "¥289.81/月"），与规划粗估同口径
        effective_price:
          type: string
          description: 合同优惠后的月度有效价（TradeAmount 汇总）；询价未返回 TradeAmount 时省略
        discount_source:
          type: string
          description: effective_price 显著低于 list_price 时必填，说明优惠来源（如询价结果中的合同优惠/折扣字段）；没有可核对来源时不得输出低于列表价的有效价
        deviation_ratio:
          type: number
          description: list_price 与规划粗估的比值（list_price ÷ 粗估中值），无法计算时省略并在 deviation_reason 说明
        calibers_aligned:
          type: boolean
          description: 两个估算是否属于同一计价口径（均为月度列表价且假设一致）
        deviation_reason:
          type: string
          description: calibers_aligned 为 false 或 deviation_ratio 偏离 1 超过 30% 时必填，依据 candidate.estimate_basis 说明差异来源（规格、带宽、计费方式、遗漏资源等）
    currency:
      type: string
      enum: [CNY]
    resources:
      type: array
      items:
        type: object
        required: [type, cost]
        properties:
          type:
            type: string
          cost:
            type: string
    template_fixed:
      type: boolean
    deployment_parameters:
      type: object
      description: 当前已选、已验证或已用于询价并传递给 deploying 的模板参数字典；可由后续选择或部署阶段补充覆盖
    hard_constraint_checks:
      type: array
      description: 对 candidate.hard_constraints 当前快照中每条约束的结构化验证；complete_step 由代码检查覆盖、比较、参数和证据，通过后才允许结束步骤
      items:
        type: object
        required: [constraint, status, actual_value, parameter_values, evidence]
        additionalProperties: false
        properties:
          constraint:
            type: object
            description: 从 candidate.hard_constraints 按相同 ID 原样复制的当前约束对象
            required: [id, target, property, operator, value, verification_mode, source, source_text]
            additionalProperties: false
            properties:
              id:
                type: string
                minLength: 1
                description: candidate 当前约束快照中的稳定唯一 ID
              target:
                type: string
                description: 约束对象，如 ECS、RDS、Network、Stack 或具体资源角色
              property:
                type: string
                description: 被验证的规范化属性名，如 vcpu、memory、count、region、version、bandwidth
              operator:
                type: string
                enum: [eq, ne, gt, gte, lt, lte, in, not_in, contains, not_contains]
                description: 比较操作；eq/ne 为等于/不等于，gt/gte/lt/lte 为数值范围，in/not_in 为集合包含关系，contains/not_contains 为内容包含关系
              value:
                description: 用户当前明确要求的约束值；in/not_in 使用数组
              unit:
                type: string
                description: 可选规范化单位，如 GiB、GB、Mbps、count；无单位时省略
              verification_mode:
                type: string
                enum: [direct, tool]
                description: direct 表示模板或最终参数可直接证明；tool 表示必须由本步骤真实工具结果证明
              source:
                const: user
                description: 约束来源，固定为 user
              source_text:
                type: string
                description: 产生当前约束版本的最新用户原文片段
          status:
            type: string
            enum: [satisfied, conflict, unresolved]
            description: 当前验证状态；只有 satisfied 能通过 complete_step 的代码校验
          actual_value:
            description: 归一化前的实际值，必须能够按 constraint.operator 与 constraint.value 比较
          actual_unit:
            type: string
            description: actual_value 的单位；可省略，省略时按 constraint.unit 解释；常见 CPU/内存口语单位会归一化
          parameter_values:
            type: object
            description: 为满足该约束选定的 ROS 参数子集；不映射到参数时填空对象
          evidence:
            type: array
            minItems: 1
            description: 支撑 actual_value 的可核对证据；每条证据都必须重复填写与检查一致的 actual_value
            items:
              type: object
              required: [type, summary, actual_value]
              additionalProperties: false
              properties:
                type:
                  type: string
                  enum: [context, template, tool]
                  description: 证据来源；context 为当前上下文，template 为模板内容或最终参数，tool 为本步骤真实工具调用结果
                summary:
                  type: string
                  description: 证据如何证明该实际值的简要说明
                tool_name:
                  type: string
                  description: type=tool 时真实调用的工具名，如 aliyun_api
                product:
                  type: string
                  description: 可选的云产品标识，用于绑定工具输入中的 product
                action:
                  type: string
                  description: 可选的 API Action，用于绑定工具输入中的 action
                result_path:
                  type: string
                  description: type=tool 时 actual_value 在真实工具 JSON 结果中的点分路径，数组下标使用数字
                actual_value:
                  description: 该证据实际证明的值，必须与同一检查的 actual_value 一致
              allOf:
                - if:
                    properties:
                      type:
                        const: tool
                    required: [type]
                  then:
                    required: [tool_name, result_path]
    preview_validation:
      type: object
      required: [succeeded]
      additionalProperties: false
      description: ros_preview_template 的结构化成功证明；deploying 仅在模板路径匹配且没有部署参数缺口时跳过例行校验并直接调用 ros_deploy 创建
      properties:
        succeeded:
          type: boolean
        template_url:
          type: string
        parameters:
          type: object
        region_id:
          type: string
        request_id:
          type: string
        error:
          type: string
      allOf:
        - if:
            properties:
              succeeded:
                const: true
            required: [succeeded]
          then:
            required: [template_url, parameters]
        - if:
            properties:
              succeeded:
                const: false
            required: [succeeded]
          then:
            required: [error]
    missing_deployment_parameters:
      type: array
      description: PreviewStack 或完整部署仍未补齐的参数及原因；后续选择阶段可补充，deploying 也可继续补齐
      items:
        type: object
        required: [name, reason]
        properties:
          name:
            type: string
          reason:
            type: string
    parameter_set_summary:
      type: string
    fix_summary:
      type: string
    error:
      type: string
    api_raw_summary:
      type: string
---

# ROS 模板成本预估

使用专用 ROS 模板询价工具预估部署费用。

前一步已完成模板校验；本步骤避免在成本预估前重复校验模板。首次询价前必须先尝试按参数推荐流程形成 Preview-Validated Pricing Parameter Set，不得直接跳过 PreviewStack。PreviewStack 不是成本估算的硬门禁；完整部署参数暂时无法自动形成时，仍可用当前已选参数调用 `ros_estimate_template_cost`，并把缺口留给后续步骤补充。只有在修复或改写模板后，才调用 `ros_validate_template` 校验改动。

## 执行流程

1. **解析候选与模板** — 从上下文提取 `candidate.name`、`candidate.resource_intents`、`candidate.hard_constraints` 和模板文件路径/地域；不依赖完整 intent 或 candidate
2. **提取参数** — 从模板 Parameters 中提取所有参数及其默认值
3. **推荐并预览验证询价参数** — 按「询价参数推荐与传递」完成参数推荐与预览验证，不得跳过约束求解直接编造库存值
4. **调用询价工具** — 优先使用 Preview-Validated Pricing Parameter Set；若 PreviewStack 因完整部署参数缺口无法通过，可用当前已选或可用于询价的参数调用 `ros_estimate_template_cost`
5. **按需修复问题** — 仅当询价失败且错误指向模板问题，或你必须修复/改写模板时，修改模板并写回原文件路径
6. **修改后校验并重新询价** — 调用 `ros_validate_template` 校验改动；通过后调用 `ros_estimate_template_cost` 重新询价；失败则修复重试（最多 7 轮）
7. **结构化传递参数** — 在 `complete_step.conclusion.deployment_parameters` 输出当前已选或已用于询价的参数字典；在 `preview_validation` 输出预览成功证明；在 `missing_deployment_parameters` 输出仍未补齐的完整部署参数缺口
8. **输出结果** — 汇总费用并调用 `complete_step`

## 按需校验模板

需要修复或改写模板的典型情况：
- 资源属性拼写错误或类型不匹配
- 缺少必要属性（如 VSwitch 缺少 CidrBlock）
- 内置函数使用不当（如 `!Ref` 引用了不存在的资源）
- Parameters 定义不完整

校验方法：
```
ros_validate_template(
    template_url="/absolute/path/to/current-template.yml",
    region_id="cn-hangzhou",  # 已有具体地域时传；否则省略使用工具默认地域
)
```

修改后校验失败时：
1. 分析错误信息，定位问题资源/属性
2. 查阅 [references/](references/) 下的参考文件了解正确的属性和参数规范；如仍不确定 → 调用 `aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})` 查询 Schema
3. 修复模板并**写回原文件路径**（后续部署步骤从此路径读取，不写回会导致后续步骤使用错误模板）
4. 重新校验（最多 7 轮）

> **模板路径支持本地文件**：`template_url` 可传当前工作目录内的本地路径（如 `./template.yml`）。避免将大模板内容直接作为参数传递。

## 询价参数推荐与传递

缺少 Default 或上下文值时，按 [references/template-parameter-recommendation.md](references/template-parameter-recommendation.md) 的参数推荐规则求解，并通过 `ros_preview_template` 形成 **Preview-Validated Pricing Parameter Set**。不要使用 `ros_stack` 执行 `PreviewStack`；本步骤只验证参数与模板可预览，不执行部署确认或 `CreateStack`。

PreviewStack 必须传 StackName；调用 `ros_preview_template` 前，必须先确定唯一 `stack_name`。`stack_name` 使用候选方案或服务简名作为前缀，并追加时间或 6 位小写字母/数字随机串后缀（如 `ai-app-20260623-a1b2c3`），避免重名。该 `stack_name` 是预览工具参数，不写入模板 `parameters`，不放入 `deployment_parameters`。

完成上述 PreviewStack 尝试后，如果完整部署参数无法自动补齐、或预览因外部参数缺口失败，但已有参数足以询价，则可以调用 `ros_estimate_template_cost` 估算费用。软降级前必须先尽量形成完整部署参数集，不要过早把可补齐参数列入 `missing_deployment_parameters`。此时必须在 `parameter_set_summary` 说明 PreviewStack 状态，在 `missing_deployment_parameters` 列出缺口，后续选择阶段可通过 `parameter_overrides` 补充，deploying 也可继续补齐并做最终部署校验。

本步骤的裁剪规则：
- `candidate.hard_constraints` 是进入成本步骤时当前有效的完整用户硬约束快照，也是本步骤的唯一硬约束来源；不比较更早步骤的约束版本。硬约束优先级高于候选推荐、模板 Default、场景推荐和软偏好。
- 优先使用上下文已有值和模板 Default；库存相关参数缺值时，先通过 `ros_get_template_parameter_constraints` 获取合法 `AllowedValues`，必要时再按 [references/cloud-products/](references/cloud-products/) 的可用性 API 与选型策略补足。
- 每条硬约束都必须在 `hard_constraint_checks` 中原样复制 `constraint`，填写可按其 `operator` 比较的 `actual_value/actual_unit`，以及为满足它选定的 `parameter_values`。`parameter_values` 必须是最终 `deployment_parameters` 的真实子集。
- 证据来自上下文、模板或工具。每条证据都填写与检查一致的 `actual_value`。`verification_mode: direct` 可由模板或最终参数的实际值证明；`verification_mode: tool` 必须使用对应产品 reference 指定的 API，并提交 `type: tool` 的真实证据。工具证据还要填写真实 `tool_name`、`product/action` 和 API 结果的 `result_path`，不得用推测值替代。
- 不要自行输出“是否验证通过”的布尔结论。调用 `complete_step` 后，代码会逐条检查约束覆盖、status、operator/value/unit、关联参数及真实工具证据；失败结果会包含具体校验码，应按原因修正 `hard_constraint_checks`、参数或证据后重试。不得通过删除检查或放宽约束绕过代码校验。
- VpcId、VSwitchId、SecurityGroupId、KeyPairName 等已有资源参数：先查询约束或只读资源候选；API 返回候选不是编造，可作为参数候选参与回溯与 PreviewStack。没有上下文值、模板 Default、用户提供值或 API 返回候选时，才按外部输入缺失处理。
- 只能在合法候选内筛选或排序，不得编造 API 未返回的库存值；LicenseKey、Token、证书、真实域名等外部输入不得编造。不要仅因参数名是 VpcId、VSwitchId、SecurityGroupId 或 KeyPairName 就跳过参数推荐并直接停止询价。
- 对可生成参数要主动补齐：普通密码（ECS/RDS/Redis/RocketMQ/WordPress 等密码，或参数名、`NoEcho`、AssociationProperty、描述/约束表明是密码）应生成合规随机值，必须满足模板长度、复杂度、`AllowedPattern`、`ConstraintDescription`。同一个真实值必须贯穿预览、询价、`deployment_parameters`、`preview_validation.parameters` 和 `complete_step.conclusion`，不得写入 `***`、`[REDACTED]` 或 `<redacted>`；服务端日志由运行时单独脱敏。
- `PreviewStack` 因候选组合不可行失败时，按 reference 的回溯规则更换候选；因外部输入缺失失败时，记录缺口，不用占位值伪造，并按上方软门禁规则决定是否继续询价。
- 最终得到的参数集不写入模板 `Default`；将当前已选、已验证或已用于询价的参数作为结构化数据放入 `complete_step.conclusion.deployment_parameters`，传递给 deploying。`ros_preview_template` 成功时，还必须把 `succeeded: true`、同一个 `template_url` 和预览时使用的 `parameters` 写入 `complete_step.conclusion.preview_validation`；deploying 用它判断同一模板是否已完成预览验证，实际部署参数由 `ros_deploy` 做最终校验。模板 Default 只是参数求解的输入来源之一，不是跨步骤传参介质。
- PreviewStack 成功但询价失败时，不要丢弃 Preview-Validated Pricing Parameter Set；仍在 `deployment_parameters` 输出该参数集，同时如实报告询价失败原因。

## 调用询价 API

通过 `template_url` 传递模板文件路径（不要用 `TemplateBody` 内联模板内容，模板可能很大）。`parameters` 直接传字典格式；不要手动展开：

```python
ros_estimate_template_cost(
    template_url="./ros-template.yml",
    parameters={
        "ZoneId": "cn-hangzhou-k",
        "InstanceType": "ecs.g7.large",
        "ImageId": "centos_stream_9_x64_20G_alibase_20260414.vhd",
        "SystemDiskCategory": "cloud_essd",
    },
    region_id="cn-hangzhou",
)
```

参数值来源：
- `hard_constraints` 中用户明确指定的规格/参数 → 按通用 operator、value、unit 做不可放宽的约束求解
- 上下文中已有部署/可用性选择结果且不违反用户硬约束的 → 使用上下文值
- 模板 Parameters 中有 Default 值且上下文未覆盖的 → 使用默认值
- 没有 Default 的库存相关参数（ZoneId、InstanceType 等）→ 按「询价参数推荐与传递」求解，不要直接编造
- PreviewStack 成功时，最终用于询价的参数集必须与 PreviewStack 验证通过的参数集一致；PreviewStack 未通过但继续询价时，`deployment_parameters` 填当前已用于询价的参数，`missing_deployment_parameters` 填完整部署参数缺口

## ROS 模板修复参考

修复模板时，查阅以下参考文件获取详细信息：

| 文件 | 内容 | 何时查阅 |
|------|------|----------|
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md、ga.md） | 需要了解产品属性、规格选型、库存相关字段时 |
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 | 修复 Parameters 定义（缺少 AssociationProperty、Label 等）时 |
| [references/ros-template.md](references/ros-template.md) | ROS 模板最佳实践：RunCommand、嵌套栈、条件部署 | 修复资源定义、内置函数用法等模板结构问题时 |

### 查询资源属性 Schema

不确定资源属性时：
```
aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})
```

## 重要约束

- **必须**使用 `ros_get_template_parameter_constraints`、`ros_preview_template`、`ros_estimate_template_cost`、`ros_validate_template` 处理 ROS 模板参数约束、预览、询价和校验；不要直接调用 `aliyun_api` 的对应 ROS 模板 API
- **不要**搜索定价文档或使用 `aliyun_doc_search`
- **不要**使用 bash 执行本地命令
- 询价失败时报告错误原因，不要编造费用数据
- 修复模板后**必须写回原文件路径** — 后续部署步骤直接使用此文件，未写回等于向下游传递错误模板
- 修改后校验不通过时**不要跳过修复直接询价**，错误模板会导致后续部署失败

## 输出
调用 `complete_step` 提交结论。字段定义见 tool schema。

补充说明：
- `cost` 字段为字符串，包含金额和计费周期（如 "¥800/月"、"¥0.5/小时"、"¥0"）
- 若修复了模板，设置 `template_fixed: true` 并在 `fix_summary` 中说明修复内容；仅形成或输出 `deployment_parameters` 不算模板修复
- `deployment_parameters` 填当前已选、已验证或已用于 `ros_estimate_template_cost` 的参数字典；PreviewStack 成功但询价失败时仍填该参数集；没有任何可用参数时填 `{}`
- 没有硬约束时，`hard_constraint_checks` 填 `[]`；不要输出 `hard_constraints_verified`
- `preview_validation` 填 `ros_preview_template` 的结构化状态：成功时填 `{"succeeded": true, "template_url": "<当前模板文件路径>", "parameters": <预览通过的同一参数字典>}`；失败或未执行时填 `{"succeeded": false, "error": "<原因>"}`
- `missing_deployment_parameters` 填完整部署或 PreviewStack 仍缺少的参数及原因；没有缺口时可省略或填 `[]`
- `parameter_set_summary` 可简要说明参数来源、可用性筛选、PreviewStack 验证结果以及是否使用软门禁继续询价
- 询价失败时 `monthly_estimate` 填 "询价失败"，`resources` 为空数组，`error` 说明原因

## 价格口径对照

`pricing_calibers` 是规划粗估与最终询价的对照结论，由代码在 `complete_step` 时校验：

- `planning_estimate` 原样复制 `candidate.monthly_estimate`（架构规划的粗略估算，列表价口径）；架构规划未给出粗估时填 `"未提供"`。
- `list_price` 填本次询价的月度列表价（`OriginalAmount` 汇总），必须与粗估同口径才可比。
- `deviation_ratio` 填 `list_price ÷ 粗估中值`；`calibers_aligned` 说明两者是否同口径。
- `deviation_ratio` 偏离 1 超过 30%，或 `calibers_aligned` 为 false 时，必须在 `deviation_reason` 依据 `candidate.estimate_basis` 说明差异来源（实例规格、磁盘、公网带宽、计费方式、粗估遗漏的资源等），不得留空。
- `effective_price` 填合同优惠后的月度有效价（`TradeAmount` 汇总）。有效价显著低于列表价时，`discount_source` 必须说明询价结果中的优惠来源；**没有可核对来源时不得输出低于列表价的有效价**，尤其不得输出无来源的 `¥0.00/月`，此时只保留列表价并在 `api_raw_summary` 说明缺失字段。
- 与硬约束校验一致：不要自行输出"口径是否一致"以外的布尔结论；代码校验失败时会返回具体校验码（如 `deviation_reason_missing`、`zero_effective_price_without_source`），按原因补全对照后重试，不得删除字段绕过校验。
