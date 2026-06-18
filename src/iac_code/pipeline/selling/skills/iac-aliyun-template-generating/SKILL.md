---
name: iac-aliyun-template-generating
description: 阿里云 ROS 模板生成——将架构方案转化为可部署的 ROS YAML 模板
when_to_use: 当需要根据架构方案生成阿里云 ROS 模板时
user_invocable: false
conclusion_schema:
  type: object
  required: [template, file_path, region, description]
  additionalProperties: false
  properties:
    template:
      type: string
      description: 与写入文件相同的 YAML 字符串
    file_path:
      type: string
      description: 模板文件路径
    region:
      type: string
      description: 部署地域
    description:
      type: string
      description: 模板简要描述
---

# ROS 模板生成

将架构方案转化为阿里云 ROS（资源编排服务）YAML 模板。

## 地域

所有 API 调用都需要地域，按以下优先级确定：
1. **用户指定**（如"在北京创建"）→ 使用用户指定的地域
2. **工具默认地域**（用户未指定时）→ aliyun_api 工具的 region_id 参数描述中会显示默认地域（如 `Defaults to 'cn-hangzhou'`），使用该默认值并告知用户
3. **均无**（工具参数无默认值且用户未指定）→ 请用户指定目标地域

**注意**：ROS 的模板、资源类型、模块是全局资源，任意地域查询结果相同。不要遍历地域列表。

## 模板生成流程

1. 分析架构方案，确定资源列表
2. 查阅 [references/cloud-products/](references/cloud-products/) 下对应产品文件，了解选型策略和库存相关属性
3. 生成 ROS YAML 模板（库存相关属性按「参数化规则」定义为 Parameters，所有 Parameters 必须添加 AssociationProperty）并写入文件
4. 调用 aliyun_api(product="ros", action="ValidateTemplate", params={"TemplateURL": <模板文件路径>}) 校验
5. 校验失败 → 分析错误 → 修复 → 重试（最多 5 轮）
6. 校验通过 → 完成

> **TemplateURL 支持本地文件路径**：aliyun_api（product=ros）中，TemplateURL 可传本地文件路径（如 `/tmp/template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 资源生命周期约束

候选架构可能包含 `resource_intents`。该字段优先级高于自然语言描述：

- `resource_intents` 中 `action=create` 的资源才允许出现在 ROS `Resources` 中作为新建资源。
- action=use_existing/reference 的资源必须建模为 Parameters 或外部引用，不得在 Resources 中创建。例如“已有 VPC 中创建安全组”时，应定义 `VpcId` Parameter，并让 SecurityGroup 的 `VpcId` 引用该参数。
- `action=forbid` 的资源不得在模板中创建；除非用户明确要求引用已有资源，也不要生成相关 Parameter。
- 如果 candidate 的自然语言、products 和生命周期字段冲突，以生命周期字段为准；冲突严重无法生成时，通过 rollback_request 回到 architecture_planning。

示例：`resource_intents: [{"product": "SecurityGroup", "action": "create"}, {"product": "VPC", "action": "use_existing"}]` 时，只生成 `ALIYUN::ECS::SecurityGroup`，不要生成 `ALIYUN::ECS::VPC` 或 `ALIYUN::ECS::VSwitch`。

## 参数化规则

生成模板时，以下属性**必须**定义为 Parameters（部署前通过 API 查询确定实际值）：

| 产品 | 须参数化的属性 |
|------|---------------|
| ECS | ZoneId, InstanceType, ImageId, SystemDiskCategory, DataDiskCategory |
| RDS | ZoneId, DBInstanceClass, DBInstanceStorageType |
| Redis | ZoneId, InstanceClass |
| SLB/ALB | ZoneId |

以下属性**不需要**参数化，直接使用合理默认值：
- 网络：VPC CIDR、VSwitch CIDR
- 命名：实例名称、资源名称
- 安全：安全组规则
- 配置：备份策略、监控设置、标签

## 资源命名

资源名称应体现业务用途，**不要**包含工具名（如 ros）：
- 好：`my-vpc`、`web-server`、`app-db`
- 差：`ros-ecs`、`ros-vpc`

## 生成要求

- 对用户未指定的参数直接使用合理默认值，不反复询问
- 库存相关属性必须参数化为 Parameters，不写死具体值
- 模板格式为 YAML
- 使用 `!Ref`、`!GetAtt` 等内置函数引用参数和资源属性，避免硬编码
- Outputs 中所有输出变量必须定义 Label

## 常用资源类型

- ALIYUN::ECS::VPC: 创建专有网络
- ALIYUN::ECS::VSwitch: 创建交换机
- ALIYUN::ECS::SecurityGroup: 创建安全组
- ALIYUN::ECS::InstanceGroup: 创建 N 个 ECS 实例（通过 `MaxAmount` 指定数量）
- ALIYUN::ECS::RunCommand: 在实例中执行自定义命令
- ALIYUN::ECS::Invocation: 执行公共命令

## 在实例中执行命令

**不要使用 UserData + WaitCondition**。根据场景选择：

- **自定义命令** → `ALIYUN::ECS::RunCommand` + `CommandContent`
- **公共命令** → `ALIYUN::ECS::Invocation` + `CommandName`

## 资源和文档搜索

- 不确定的资源属性或 Schema → aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})
- 不熟悉的资源类型/属性 → aliyun_doc_search（category_id=28850）
- 摘要不够 → web_fetch 获取完整文档

## 错误处理

### 校验失败
分析错误原因 → 查 GetResourceType Schema（如需）→ 修复 → 重试（最多 5 轮）

## 参考文件

| 文件 | 内容 |
|------|------|
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 |
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） |
| [references/ros-template.md](references/ros-template.md) | ROS 模板最佳实践：RunCommand、嵌套栈、条件部署 |
