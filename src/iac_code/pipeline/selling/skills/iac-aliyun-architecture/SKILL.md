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
        required: [name, output_path, products, topology, monthly_estimate, pros, cons]
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

「简单明确」指的是**资源清单和生命周期都已确定**。如果意图里的资源本身很少，但某个依赖资源是新建还是复用尚未确认，那它不属于简单明确需求，必须按下面「依赖资源生命周期未确认时的候选覆盖」给出多个候选，不能因为资源数量少就收敛成单候选。

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

### 未声明的资源默认是新建

`use_existing` / `reference` 只能来自 intent 的显式声明，不能由你推断：

- intent 的 `resource_intents` 没有把某个 product 标成 `use_existing` 或 `reference` 时，该资源默认按 `action=create` 处理。
- intent 完全没有 `resource_intents` 字段时，`core_requirements` 里的每个 product 都默认按 `action=create` 处理。
- 不得因为某个资源“通常已经存在”“一般由用户提前准备好”就预设已有资源。没有显式声明就当作本次新建。

反例：intent 只解析出 `VPC`、`VSwitch` 且没有任何 `use_existing` 声明时，把 VPC 预设成已有 VPC、只产出「已有 VPC 下新建 VSwitch」单候选是错误的——这会让候选集完全遗漏用户的 VPC 创建需求。

### 依赖资源生命周期未确认时的候选覆盖

当意图里存在承载关系（如 VSwitch 依赖 VPC、ECS 依赖 VSwitch、安全组依赖 VPC），而**被依赖资源到底是新建还是复用已有还没有确认**时，必须给出至少两个候选，分别覆盖两种语义：

- 候选 A：被依赖资源 `action=create`，与目标资源一起新建（如「新建 VPC + 新建 VSwitch」）。
- 候选 B：被依赖资源 `action=use_existing`，只新建目标资源（如「复用已有 VPC + 新建 VSwitch」）。

「已确认」只有两种来源：intent 的 `resource_intents` 已显式给出该资源的 `use_existing` / `reference` / `create`，或 `non_functional.network_constraints` 已给出具体的已有资源标识（如 VPC ID、VSwitch ID、既有网段）。任一来源确认后，按确认的语义收敛为单候选，不要再为了凑数保留另一种。

每个候选的 `resource_intents` 必须与该候选自身的语义一致：候选 A 里的 VPC 是 `create`，候选 B 里的 VPC 是 `use_existing`。不要让两个候选共用同一份继承自 intent 的 `resource_intents`。

## 输出
调用 `complete_step` 提交结论。字段定义见 tool schema。

### output_path 命名规则
- 格式：`templates/{index}-{英文简写}.yml`
- index 从 1 开始
- 名称为方案名的英文 kebab-case 简写
- 示例：`templates/1-simple-nginx.yml`、`templates/2-high-availability-slb.yml`

当只有 1 个方案时，`candidates` 只有 1 个元素。只有资源清单和生命周期都已确定时才允许收敛到 1 个方案。

## 约束

- 产品组合只包含实现需求所必需的资源，不要为了"看起来完整"添加用户没需要的东西
- 费用估算基于阿里云公开定价的合理范围，不需要精确到个位
