---
name: iac-aliyun
description: 阿里云 IaC 模板生成、解释、完善与部署
when_to_use: 当用户涉及云资源创建、模板生成、模板解释、部署等 IaC 相关操作时
user_invocable: false
---

# 阿里云 IaC 技能

阿里云 IaC 模板生成、解释、完善与部署。帮助用户通过 ROS/Terraform 模板管理云资源。

## 地域

所有 API 调用都需要地域，按以下优先级确定：
1. **用户指定**（如"在北京创建"）→ 使用用户指定的地域
2. **工具默认地域**（用户未指定时）→ aliyun_api 工具的 region_id 参数描述中会显示默认地域（如 `Defaults to 'cn-hangzhou'`），使用该默认值并告知用户
3. **均无**（工具参数无默认值且用户未指定）→ 请用户指定目标地域，或使用 /auth 命令设置默认地域

确定后，所有 API 调用统一使用该地域。

**注意**：ROS 的模板、资源类型、模块是全局资源，任意地域查询结果相同。用户说"查所有地域的模板"时，只需查一个地域即可，不要遍历地域列表。

## 场景处理

### 生成模板
- **资源需求**（用户指定资源如"创建 VPC+ECS"）→ 直接生成模板
- **应用部署**（用户想部署某个应用但不清楚该应用是什么）→ 先 aliyun_doc_search 搜索，搜不到再 web_fetch 搜索
  - 如果部署地域属于中国，那么对 Docker、PyPI、npm、Maven、Go 等需配置国内镜像源，否则可能有网络问题等
- **业务需求**（用户描述业务场景）→ 提供 1-3 个方案含优缺点，用户选择后生成
- 默认 ROS 模板，用户指定 Terraform 时生成 Terraform 文件。**ROS 与 Terraform 共用同一套校验/部署链路（均通过 aliyun_api / ros_stack）**，不要建议用户用 `terraform init/apply` 等本地 CLI 替代
- 对用户未指定的参数直接使用合理默认值，不反复询问
- **库存相关属性必须参数化为 Parameters**，不写死具体值（见「参数化规则」）

### 解释/完善模板
- 分析用户提供的模板，解释结构和功能
- 按用户需求在已有模板上迭代完善

### 部署/更新/删除
- 所有写操作必须先向用户确认，删除/更新操作使用 ⚠️ 警告措辞
- 简洁询问是否部署，不展示工具调用细节

### 询价
- 查询部署的预估价格

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

资源名称应体现业务用途，**不要**包含工具名（如 terraform、ros）：
- 好：`my-vpc`、`web-server`、`app-db`
- 差：`vpc-terraform`、`ros-ecs`、`tf-vswitch`

## 模板生成流程

1. 分析需求，确定资源列表
2. 查阅 [references/cloud-products/](references/cloud-products/) 下对应产品文件，了解选型策略和库存相关属性
3. 生成模板（库存相关属性按「参数化规则」定义为 Parameters，所有 Parameters 必须添加 AssociationProperty）并写入文件
   - **Terraform**：生成 `.tf` 等文件后，必须先用 `tf2ros.py` 打包为 ROS Terraform 类型模板（用法见 [references/terraform-template.md](references/terraform-template.md) 的「与 ROS 集成」节），后续步骤校验/部署的都是这份打包后的 `.yml`
4. 调用 aliyun_api(product="ros", action="ValidateTemplate", params={"TemplateURL": <模板文件路径>}) 校验
5. 校验失败 → 分析错误 → 修复 → 重试（最多 5 轮）
6. 校验通过 → 展示模板 → 询问是否部署（**ROS 与 Terraform 一致**，禁止用 `terraform init/apply` 等本地 CLI 步骤替代部署确认）

> **TemplateURL 支持本地文件路径**：aliyun_api（product=ros）和 ros_stack 中，TemplateURL 可传本地文件路径（如 `/tmp/template.yml`），工具会自动读取文件内容。避免将大模板内容直接作为参数传递。

## 部署流程

### 可用性查询

当用户确认执行以下操作时，**必须先查询可用性**：

| 操作 | 查询范围 |
|------|----------|
| CreateStack | 全量查询所有库存相关 Parameters |
| ContinueCreateStack | 查询失败资源相关的 Parameters |
| UpdateStack | 查询变更涉及的 Parameters |
| CreateStackInstances | 按每个目标地域分别查询 |
| UpdateStackInstances | 按每个目标地域查询变更涉及的 Parameters |

查询步骤：
1. 解析模板 Parameters，识别库存相关参数及对应产品
2. 调用各产品可用性 API（具体 API 见 [references/cloud-products/](references/cloud-products/) 各产品文件的「可用性查询」节）
3. 找出公共可用区（所有资源都有库存的可用区）
4. 按 cloud-products 中的推荐规格优先匹配，不可用时选最接近的替代
5. 得到选定参数。**若操作为 CreateStack 或 UpdateStack，接「部署前询价」；其他操作（ContinueCreateStack 等）直接展示选定结果并请求用户确认。**

### 有模板的询价

1. 如果没有查询可用性，按照「可用性查询」进行
2. 调用 aliyun_api(product="ros", action="GetTemplateEstimateCost", params={...}) 询价。**模板参数必须按 `Parameters.<N>.ParameterKey` / `Parameters.<N>.ParameterValue` 平铺**（下标从 1 起），不要把参数名作为顶层 key 传入。**ROS 原生模板和 Terraform 类型模板调用同一 API，传参格式完全一致**——`ParameterKey` 即模板中的 Parameters 名（ROS）或 variable 名（Terraform，通常蛇形命名，如 `zone_id`）。示例（参数平铺通用规则见「aliyun_api 参数约定」）：
   ```python
   aliyun_api(
       product="ros",
       action="GetTemplateEstimateCost",
       params={
           "TemplateURL": "/tmp/ros-ecs-nginx-template.yml",
           "Parameters.1.ParameterKey": "zone_id",
           "Parameters.1.ParameterValue": "cn-hangzhou-k",
           "Parameters.2.ParameterKey": "instance_type",
           "Parameters.2.ParameterValue": "ecs.g7.large",
           "Parameters.3.ParameterKey": "image_id",
           "Parameters.3.ParameterValue": "centos_stream_9_x64_20G_alibase_20260414.vhd",
           "Parameters.4.ParameterKey": "system_disk_category",
           "Parameters.4.ParameterValue": "cloud_essd",
       },
       region_id="cn-hangzhou",
   )
   ```
3. 合并展示「选定参数 + 预估费用」（格式见「询价展示格式」），一次性请求用户确认
4. **UpdateStack 特别提示**：展示时附加 `此为更新后模板的总费用预估，而非变更前后价差`
5. 用户确认 → 进入「执行部署」；用户拒绝 → 终止并告知已取消部署

**询价失败**：不阻塞部署。按如下格式展示后继续征求部署确认：
```
为您选定以下参数：
- 可用区：cn-beijing-h
- ECS 实例规格：ecs.g7.large (2c8g)
- ...

预估费用（按量付费）：
- web-server (ECS ecs.g7.large): ¥xx/h
- app-db (RDS mysql.n4.large.1): ¥xx/h
- 其他（VPC / 安全组 / 交换机 等）: 免费
合计: ¥xx/h

⚠️ 询价失败: <错误简述>
  常见原因: 部分资源类型不支持询价 / 账号询价权限缺失 / API 暂时异常

确认部署？
```
6. 用户确认 → 将选定值作为 Parameters 传入部署操作

无法找到公共可用区时，告知用户冲突详情，建议换规格系列或换地域。

### 执行部署

- 使用 ros_stack 工具执行 CreateStack/UpdateStack/ContinueCreateStack/DeleteStack，禁止用 Bash
- CreateStack 必须传 `DisableRollback: true`

## 参考文件

| 文件 | 内容 |
|------|------|
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组（ROS/Terraform 共用） |
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） |
| [references/ros-template.md](references/ros-template.md) | ROS 原生模板最佳实践：RunCommand、嵌套栈、条件部署 |
| [references/terraform-template.md](references/terraform-template.md) | Terraform 最佳实践：文件组织、变量、Data Source、ROS 集成 |

## 资源和文档搜索

- 不确定的资源属性或Schema：
  - ROS → aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})
  - Terraform → aliyun_api(product="IaCService", action="GetResourceType", style="ROA", method="GET", pathname="/resourceType/<类型>")
- 不熟悉的资源类型/属性 → aliyun_doc_search（ROS 传 category_id=28850，Terraform 传 category_id=95817）
- 想要了解应用部署方案、解决方案、云产品相关知识 -> aliyun_doc_search
- 摘要不够 → web_fetch 获取完整文档

## aliyun_api 参数约定

**以下规则仅适用于 RPC 风格 API**（`style` 未传或传 `"RPC"`；ROA 风格用 JSON body/query，不受此约束）。

调用 RPC API 时，**array、object 类参数需平铺为带数字下标的键**，工具不会自动展开。规则：

- 下标从 `1` 起，依次递增
- `array[string]` → `<Name>.<N>`
- `array[object]` → `<Name>.<N>.<SubKey>`
- 嵌套列表按同样规则继续展开
- `object` → `<Name>.<SubKey>`

## 错误处理

### 校验失败
分析错误原因 → 查 GetResourceType Schema（如需）→ 修复 → 重试（最多 5 轮）

### 部署失败
分析错误原因：
- 权限/配额 → 告知用户处理
- 模板/参数 → 修复后 ContinueCreateStack（不重新 CreateStack）
