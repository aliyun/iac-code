---
name: iac-aliyun-cost
description: 使用 ROS GetTemplateEstimateCost API 预估 ROS 模板的月度部署费用，支持按需修复和校验模板问题
when_to_use: 当需要对阿里云 ROS 模板进行费用预估时
user_invocable: false
conclusion_schema:
  type: object
  required: [monthly_estimate, currency, resources, template_fixed]
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
    fix_summary:
      type: string
    error:
      type: string
    api_raw_summary:
      type: string
---

# ROS 模板成本预估

使用阿里云 ROS `GetTemplateEstimateCost` API 预估部署费用。

前一步已完成模板校验；本步骤先直接询价，避免在成本预估前重复校验。只有在修复或改写模板后，才调用 `ValidateTemplate` 校验改动。

## 执行流程

1. **解析模板** — 从上下文的 `template` 字段获取模板内容和文件路径
2. **提取参数** — 从模板 Parameters 中提取所有参数及其默认值
3. **调用询价 API** — 使用 `GetTemplateEstimateCost` 获取费用预估
4. **按需修复问题** — 仅当询价失败且错误指向模板问题，或你必须修复/改写模板时，修改模板并写回原文件路径
5. **修改后校验并重新询价** — 调用 `ValidateTemplate` 校验改动；通过后调用 `GetTemplateEstimateCost` 重新询价；失败则修复重试（最多 7 轮）
6. **输出结果** — 汇总费用并调用 `complete_step`

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

## 调用询价 API

通过 `TemplateURL` 传递模板文件路径（不要用 `TemplateBody` 内联模板内容，模板可能很大）。模板参数必须按 `Parameters.<N>.ParameterKey` / `Parameters.<N>.ParameterValue` 平铺（下标从 1 起），不要把参数名作为顶层 key 传入：

```python
aliyun_api(
    product="ros",
    action="GetTemplateEstimateCost",
    params={
        "TemplateURL": "/tmp/ros-template.yml",
        "Parameters.1.ParameterKey": "ZoneId",
        "Parameters.1.ParameterValue": "cn-hangzhou-k",
        "Parameters.2.ParameterKey": "InstanceType",
        "Parameters.2.ParameterValue": "ecs.g7.large",
        "Parameters.3.ParameterKey": "ImageId",
        "Parameters.3.ParameterValue": "centos_stream_9_x64_20G_alibase_20260414.vhd",
        "Parameters.4.ParameterKey": "SystemDiskCategory",
        "Parameters.4.ParameterValue": "cloud_essd",
    },
    region_id="cn-hangzhou",
)
```

参数值来源：
- 模板 Parameters 中有 Default 值的 → 使用默认值
- 没有默认值的库存相关参数（ZoneId、InstanceType 等）→ 使用上下文中提供的值或合理默认值

## ROS 模板修复参考

修复模板时，查阅以下参考文件获取详细信息：

| 文件 | 内容 | 何时查阅 |
|------|------|----------|
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） | 需要了解产品属性、规格选型、库存相关字段时 |
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 | 修复 Parameters 定义（缺少 AssociationProperty、Label 等）时 |
| [references/ros-template.md](references/ros-template.md) | ROS 模板最佳实践：RunCommand、嵌套栈、条件部署 | 修复资源定义、内置函数用法等模板结构问题时 |

### 参数化规则

以下属性必须定义为 Parameters：

| 产品 | 须参数化的属性 |
|------|---------------|
| ECS | ZoneId, InstanceType, ImageId, SystemDiskCategory, DataDiskCategory |
| RDS | ZoneId, DBInstanceClass, DBInstanceStorageType |
| Redis | ZoneId, InstanceClass |
| SLB/ALB | ZoneId |

### 常用资源类型

- ALIYUN::ECS::VPC — 专有网络
- ALIYUN::ECS::VSwitch — 交换机
- ALIYUN::ECS::SecurityGroup — 安全组
- ALIYUN::ECS::InstanceGroup — ECS 实例（通过 MaxAmount 指定数量）
- ALIYUN::RDS::DBInstance — RDS 数据库实例
- ALIYUN::REDIS::Instance — Redis 缓存实例
- ALIYUN::SLB::LoadBalancer — 负载均衡

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
- 若修复了模板，设置 `template_fixed: true` 并在 `fix_summary` 中说明修复内容
- 询价失败时 `monthly_estimate` 填 "询价失败"，`resources` 为空数组，`error` 说明原因
