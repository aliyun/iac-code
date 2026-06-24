---
name: iac-aliyun-cost
description: 使用 ROS GetTemplateEstimateCost API 预估 ROS 模板的月度部署费用，支持按需修复和校验模板问题
when_to_use: 当需要对阿里云 ROS 模板进行费用预估时
user_invocable: false
conclusion_schema:
  type: object
  required: [monthly_estimate, currency, resources, template_fixed, deployment_parameters]
  additionalProperties: false
  properties:
    monthly_estimate:
      type: string
      description: 月度费用估算（如 ¥1500/月）或 "询价失败"
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
      description: 当前已选、已验证或已用于询价并传递给 deploying 的模板参数字典；可由后续选择阶段补充覆盖
    missing_deployment_parameters:
      type: array
      description: PreviewStack 或最终部署仍缺少、需要用户在后续选择阶段补充的参数
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

使用阿里云 ROS `GetTemplateEstimateCost` API 预估部署费用。

前一步已完成模板校验；本步骤避免在成本预估前重复校验模板。首次询价前优先按参数推荐流程形成 Preview-Validated Pricing Parameter Set，再调用询价 API。PreviewStack 不是硬门禁；完整部署参数暂时无法自动形成时，仍可用当前已选参数调用询价 API，并把缺口留给后续选择阶段补充。只有在修复或改写模板后，才调用 `ValidateTemplate` 校验改动。

## 执行流程

1. **解析模板** — 从上下文的 `template` 字段获取模板内容和文件路径
2. **提取参数** — 从模板 Parameters 中提取所有参数及其默认值
3. **推荐并预览验证询价参数** — 按「询价参数推荐与传递」优先形成 Preview-Validated Pricing Parameter Set，不得跳过约束求解直接编造库存值
4. **调用询价 API** — 优先使用 Preview-Validated Pricing Parameter Set；若 PreviewStack 因完整部署参数缺口无法通过，可用当前已选或可用于询价的参数调用 `GetTemplateEstimateCost`
5. **按需修复问题** — 仅当询价失败且错误指向模板问题，或你必须修复/改写模板时，修改模板并写回原文件路径
6. **修改后校验并重新询价** — 调用 `ValidateTemplate` 校验改动；通过后调用 `GetTemplateEstimateCost` 重新询价；失败则修复重试（最多 7 轮）
7. **结构化传递参数** — 在 `complete_step.conclusion.deployment_parameters` 输出当前已选或已用于询价的参数字典；在 `missing_deployment_parameters` 输出仍需用户补充的完整部署参数缺口
8. **输出结果** — 汇总费用并调用 `complete_step`

## 按需校验模板

需要修复或改写模板的典型情况：
- 资源属性拼写错误或类型不匹配
- 缺少必要属性（如 VSwitch 缺少 CidrBlock）
- 内置函数使用不当（如 `!Ref` 引用了不存在的资源）
- Parameters 定义不完整

校验方法：
```
aliyun_api(
    product="ros",
    action="ValidateTemplate",
    params={"TemplateURL": "<模板文件路径>"},
    region_id="<地域>"
)
```

修改后校验失败时：
1. 分析错误信息，定位问题资源/属性
2. 查阅 [references/](references/) 下的参考文件了解正确的属性和参数规范；如仍不确定 → 调用 `aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})` 查询 Schema
3. 修复模板并**写回原文件路径**（后续部署步骤从此路径读取，不写回会导致后续步骤使用错误模板）
4. 重新校验（最多 7 轮）

> **TemplateURL 支持本地文件路径**：`TemplateURL` 可传本地路径（如 `/tmp/template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 询价参数推荐与传递

缺少 Default 或上下文值时，按 [references/template-parameter-recommendation.md](references/template-parameter-recommendation.md) 的参数推荐规则求解，并优先通过 `aliyun_api(product="ros", action="PreviewStack")` 形成 **Preview-Validated Pricing Parameter Set**。不要使用 `ros_stack` 执行 `PreviewStack`；本步骤只验证参数与模板可预览，不执行部署确认或 `CreateStack`。

PreviewStack 必须传 StackName；调用 PreviewStack 前，必须先确定唯一 `StackName` 并传入 `PreviewStack` 参数。`StackName` 使用候选方案或服务简名作为前缀，并追加时间或 6 位小写字母/数字随机串后缀（如 `ai-app-20260623-a1b2c3`），避免重名。该 `StackName` 是 ROS API 参数，不写入模板 `Parameters`，不放入 `deployment_parameters`。

PreviewStack 不是硬门禁。它要求完整部署参数，常比 `GetTemplateEstimateCost` 需要更多外部输入；如果完整部署参数无法自动补齐、或 PreviewStack 因外部参数缺口失败，但已有参数足以询价，则可以调用 `GetTemplateEstimateCost` 估算费用。此时必须在 `parameter_set_summary` 说明 PreviewStack 状态，在 `missing_deployment_parameters` 列出缺口，后续选择阶段可通过 `parameter_overrides` 补齐，deploying 再做最终部署校验。

本步骤的裁剪规则：
- 优先使用上下文已有值和模板 Default；库存相关参数缺值时，先通过 `GetTemplateParameterConstraints` 获取合法 `AllowedValues`，必要时再按 [references/cloud-products/](references/cloud-products/) 的可用性 API 与选型策略补足。
- VpcId、VSwitchId、SecurityGroupId、KeyPairName 等已有资源参数：先查询约束或只读资源候选；API 返回候选不是编造，可作为参数候选参与回溯与 PreviewStack。没有上下文值、模板 Default、用户提供值或 API 返回候选时，才按外部输入缺失处理。
- 只能在合法候选内筛选或排序，不得编造 API 未返回的库存值；LicenseKey、Token、证书、真实域名等外部输入不得编造。不要仅因参数名是 VpcId、VSwitchId、SecurityGroupId 或 KeyPairName 就跳过参数推荐并直接停止询价。
- `PreviewStack` 因候选组合不可行失败时，按 reference 的回溯规则更换候选；因外部输入缺失失败时，记录缺口，不用占位值伪造，并按上方软门禁规则决定是否继续询价。
- 最终得到的参数集不写入模板 `Default`；将当前已选、已验证或已用于询价的参数作为结构化数据放入 `complete_step.conclusion.deployment_parameters`，传递给 deploying。模板 Default 只是参数求解的输入来源之一，不是跨步骤传参介质。
- PreviewStack 成功但询价失败时，不要丢弃 Preview-Validated Pricing Parameter Set；仍在 `deployment_parameters` 输出该参数集，同时如实报告询价失败原因。

## 调用询价 API

通过 `TemplateURL` 传递模板文件路径（不要用 `TemplateBody` 内联模板内容，模板可能很大）。ROS API 的 `Parameters` 直接传字典格式，工具会自动展开为 API 所需的平铺参数；不要手动展开：

```python
aliyun_api(
    product="ros",
    action="GetTemplateEstimateCost",
    params={
        "TemplateURL": "/tmp/ros-template.yml",
        "Parameters": {
            "ZoneId": "cn-hangzhou-k",
            "InstanceType": "ecs.g7.large",
            "ImageId": "centos_stream_9_x64_20G_alibase_20260414.vhd",
            "SystemDiskCategory": "cloud_essd",
        },
    },
    region_id="cn-hangzhou",
)
```

参数值来源：
- 上下文中已有部署/可用性选择结果的 → 使用上下文值
- 模板 Parameters 中有 Default 值且上下文未覆盖的 → 使用默认值
- 没有 Default 的库存相关参数（ZoneId、InstanceType 等）→ 按「询价参数推荐与传递」求解，不要直接编造
- PreviewStack 成功时，最终用于询价的参数集必须与 PreviewStack 验证通过的参数集一致；PreviewStack 未通过但继续询价时，`deployment_parameters` 填当前已用于询价的参数，`missing_deployment_parameters` 填完整部署参数缺口

## ROS 模板修复参考

修复模板时，查阅以下参考文件获取详细信息：

| 文件 | 内容 | 何时查阅 |
|------|------|----------|
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） | 需要了解产品属性、规格选型、库存相关字段时 |
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 | 修复 Parameters 定义（缺少 AssociationProperty、Label 等）时 |
| [references/ros-template.md](references/ros-template.md) | ROS 模板最佳实践：RunCommand、嵌套栈、条件部署 | 修复资源定义、内置函数用法等模板结构问题时 |

### 查询资源属性 Schema

不确定资源属性时：
```
aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})
```

## 重要约束

- **必须**使用 `aliyun_api` 工具调用 ROS API — 不要自行估算费用
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
- `deployment_parameters` 填当前已选、已验证或已用于 `GetTemplateEstimateCost` 的参数字典；PreviewStack 成功但询价失败时仍填该参数集；没有任何可用参数时填 `{}`
- `missing_deployment_parameters` 填完整部署或 PreviewStack 仍缺少的参数及原因；没有缺口时可省略或填 `[]`
- `parameter_set_summary` 可简要说明参数来源、可用性筛选、PreviewStack 验证结果以及是否使用软门禁继续询价
- 询价失败时 `monthly_estimate` 填 "询价失败"，`resources` 为空数组，`error` 说明原因
