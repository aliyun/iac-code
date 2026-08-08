---
name: iac-aliyun-architecture
description: 根据用户意图生成差异化候选架构方案，简单需求直接给出唯一方案
conclusion_schema:
  type: object
  required: [candidates]
  additionalProperties: false
  properties:
    candidates:
      type: array
      minItems: 1
      items:
        type: object
        required: [name, output_path, products, hard_constraints, topology, monthly_estimate, pros, cons]
        properties:
          name:
            type: string
            description: 方案名称（体现核心差异）
          output_path:
            type: string
            description: "模板文件路径，格式: templates/{index}-{kebab-case-name}.yml"
          products:
            type: array
            items:
              type: string
          resource_intents:
            type: array
            items:
              type: object
              required: [product, action]
              additionalProperties: false
              properties:
                product:
                  type: string
                action:
                  type: string
                  enum: [create, use_existing, reference, forbid]
                role:
                  type: string
                source:
                  type: string
                notes:
                  type: string
            description: 本方案中每个资源的新建、复用、引用或禁止语义；从 intent.resource_intents 继承或收窄
          hard_constraints:
            type: array
            description: 当前候选采用的用户硬约束完整快照；以进入本步骤后的最新用户要求为准，不得加入推断规格或方案推荐值
            items:
              type: object
              required: [id, target, property, operator, value, verification_mode, source, source_text]
              additionalProperties: false
              properties:
                id:
                  type: string
                  minLength: 1
                  description: 当前请求内稳定且唯一的约束标识；用户修改同一约束时保持 ID，内容更新为最新要求
                target:
                  type: string
                  description: 约束对象，如 ECS、RDS、Network、Stack 或具体资源角色
                property:
                  type: string
                  description: 规范化属性名，如 vcpu、memory、count、region、version、bandwidth、stack_name
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
                  description: direct 表示可由模板或最终参数直接证明；tool 表示必须查询云产品元数据、库存或已有资源才能证明
                source:
                  const: user
                  description: 约束来源；硬约束只能来自用户明确表达，因此固定为 user
                source_text:
                  type: string
                  description: 产生当前约束版本的最新用户原文片段
          topology:
            type: string
          monthly_estimate:
            type: string
          pros:
            type: array
            items:
              type: string
          cons:
            type: array
            items:
              type: string
---

# 架构规划

根据前一步提取的用户意图，设计阿里云架构方案供用户选择。

## 核心原则：按需设计，不过度发挥

方案数量取决于需求复杂度，而非固定出 2-3 个凑数：

- **简单明确的需求**（如"创建一个 VPC"、"建一个 OSS bucket"）：只给 1 个方案，不要画蛇添足地加资源。用户要什么就设计什么，不需要提供替代方案。
- **有设计空间的需求**（如"部署一个 Web 应用"、"搭建微服务架构"）：给出 2-3 个有实质差异的方案。差异必须来自用户需求中隐含的取舍，而非凭空制造。

判断标准：如果你需要添加用户完全没提到的产品来"制造"差异，那就不该有多个方案。

若将 iac-code Web 部署到 ECS，只生成一个单 ECS + EIP 候选，安全组仅开放 8766，不得增加其他入口资源；
将其 `candidate.name` 固定为 `iac-code-web-single-ecs`。

## 差异化维度

当需求确实存在设计取舍时，根据场景从以下维度中选择最相关的来构建差异方案：

| 维度 | 适用场景 | 示例 |
|------|---------|------|
| 成本梯度 | 用户未明确预算，需求可高可低配 | 开发环境 vs 生产环境规格 |
| 可用性级别 | 业务关键程度不明确 | 单可用区 vs 多可用区冗余 |
| 托管 vs 自建 | 同一能力有托管服务和自建方案 | RDS vs 自建 MySQL on ECS |
| 架构模式 | 业务规模和演进方向不确定 | 单体 vs 微服务、同步 vs 异步 |
| Serverless vs 传统 | 流量模式不确定 | FC + API Gateway vs ECS + SLB |
| 弹性策略 | 负载是否可预测 | 固定规格 vs 弹性伸缩组 |
| 数据方案 | 数据量级/访问模式不明确 | 单实例 RDS vs 读写分离 vs PolarDB |

不要机械地套用上表。选维度的依据是用户意图中实际存在的不确定性——哪里有取舍，就在哪里提供选择。

## 每个方案包含

- 方案名称（体现核心差异，如"Serverless 轻量方案"而非泛泛的"方案一"）
- 核心阿里云产品组合（只列必要的产品）
- 资源生命周期：`resource_intents`
- 拓扑描述（简述部署架构）
- 月度费用估算范围
- 优势和局限

## 资源生命周期约束

如果 intent 中存在 `resource_intents`，它是架构设计的硬约束：

- 只有 `action=create` 的资源可以作为本方案要新建的资源。不要把 `action=use_existing` 或 `action=reference` 的资源设计成新建资源。
- `action=use_existing/reference` 必须作为已有资源引用，后续模板中应通过参数（如 `VpcId`）或用户提供 ID 引用，不得生成对应的新建资源。换句话说，use_existing/reference 必须作为已有资源引用。
- `action=forbid` 的资源不得出现在候选方案的新增资源里，也不得作为“顺手补齐”的依赖加入。
- 将 `resource_intents` 原样或按方案收窄后写入每个 candidate，供模板生成步骤继续执行同一约束。

示例：intent 表示“已有 VPC 中创建安全组”时，candidate 应包含 `resource_intents: [{"product": "VPC", "action": "use_existing"}, {"product": "SecurityGroup", "action": "create"}]`。不得生成 VSwitch，也不得设计成“创建 VPC + VSwitch + SecurityGroup”。

## 用户硬约束

以 `intent.hard_constraints` 为起点，并应用用户在进入或执行本步骤后明确提出的修改，将当前有效的完整约束快照写入每个 candidate 的 `hard_constraints`，包括 `verification_mode`。同一约束被用户修改时保留稳定 `id` 并更新其它字段；用户明确删除约束时从快照中删除。不得把推断规格或本方案推荐值新增为用户硬约束。方案差异只能发生在当前硬约束允许的空间内；某方案无法满足时不要生成该方案。

## 输出
调用 `complete_step` 提交结论。字段定义见 tool schema。

### output_path 命名规则
- 格式：`templates/{index}-{英文简写}.yml`
- index 从 1 开始
- 名称为方案名的英文 kebab-case 简写
- 示例：`templates/1-simple-nginx.yml`、`templates/2-high-availability-slb.yml`

当只有 1 个方案时，`candidates` 只有 1 个元素。

## 约束

- 产品组合只包含实现需求所必需的资源，不要为了"看起来完整"添加用户没需要的东西
- 费用估算基于阿里云公开定价的合理范围，不需要精确到个位
