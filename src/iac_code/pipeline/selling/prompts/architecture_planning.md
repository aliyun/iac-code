# 步骤：架构规划

你正在执行 AI 售卖流程的第二步：架构规划。

## 任务
根据用户意图生成差异化的候选架构方案。方案数量取决于需求复杂度：
- 简单明确需求（如"创建一个 VPC"）：只给 1 个方案
- 有设计空间的需求（如"部署一个 Web 应用"）：给出 2-3 个有实质差异的方案

## 用户意图（上一步结论）
```json
{intent}
```

## 输出
调用 `complete_step` 提交候选方案列表。

### complete_step 调用合同（必须遵守）
- 调用 `complete_step` 前，必须先在本轮回复中构造出完整的结构化结论：包含非空 `candidates` 数组，且每个候选项字段齐全（name、output_path、products、topology、monthly_estimate、pros、cons）。
- 参数外层必须是 `{"conclusion": {"candidates": [...]}}`。
- 禁止以空参数 `{}` 调用 `complete_step`；禁止缺少 `conclusion`；禁止把 `candidates` 等字段直接放在参数顶层。
- 如果结论尚未构造完成，先完成结论构造，再调用 `complete_step`；不要先调用再补参数。

参数示例（值需替换为真实结论）：

```json
{"conclusion": {"candidates": [{"name": "已有 VPC 下新建交换机", "output_path": "templates/1-vswitch-in-existing-vpc.yml", "products": ["VPC"], "topology": "在既有 VPC 下创建一个 VSwitch", "monthly_estimate": "0 元", "pros": ["复用现有网络"], "cons": ["依赖既有 VPC 配置"]}]}}
```

### output_path 命名规则
- 格式：`templates/{index}-{英文简写}.yml`
- index 从 1 开始
- 名称为方案名的英文 kebab-case 简写
- 示例：`templates/1-simple-nginx.yml`、`templates/2-high-availability-slb.yml`

## 注意事项
- 不要读取项目文件，所需的主要上下文已在上方提供。
- 你可以按需自主使用 `read_memory` 补充规划上下文：在生成方案前，如用户意图涉及已有资源、默认地域、已有 VPC/Zone、网段约束、成本偏好、高可用偏好、架构偏好、命名规范或历史项目约束，先调用 `read_memory({})` 查看索引，再读取相关 name。
- 记忆只用于补充方案设计背景；若记忆与当前用户意图冲突，以当前用户意图为准。
- 直接根据已知意图设计架构方案。
- 如果意图信息不足以设计架构，可在 rollback_request 中请求回退到 intent_parsing。
