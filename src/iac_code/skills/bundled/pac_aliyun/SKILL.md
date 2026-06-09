---
name: pac-aliyun
description: 阿里云 Alibaba Cloud Policy as Code / InfraGuard 合规策略生成、校验与策略库查询
when_to_use: 当用户请求阿里云/Alibaba Cloud/Alicloud 的 Policy as Code、PAC、InfraGuard、Rego 合规策略生成、策略查询、策略更新或模板合规校验时，必须先调用 skill 工具加载 pac-aliyun。
user_invocable: false
auto_trigger:
  script: auto_trigger.py
---

# 阿里云 PAC 技能

面向阿里云 ROS 模板的 Policy as Code 能力，使用 InfraGuard 进行策略查询、策略更新、模板扫描、合规策略生成与自定义 Rego 校验。

## PAC 边界

- 该技能拥有 InfraGuard、Rego、策略库、策略包、策略生成和模板合规扫描相关流程。
- `iac-aliyun` 只负责 ROS/Terraform 模板生成、解释、参数推荐、询价、部署和资源栈操作。
- 不在 iac-code 内维护 InfraGuard 官方策略副本；策略内容以 InfraGuard 官方工具和其策略更新机制为准。

## InfraGuard 懒加载

执行任何 PAC 后续能力前，先按 [references/infraguard-policy-generation.md](references/infraguard-policy-generation.md) 的 Lazy InfraGuard Sync 流程检查 InfraGuard 是否可用，并检查策略更新。

核心命令：
```bash
infraguard version
go install github.com/aliyun/infraguard/cmd/infraguard@latest
infraguard policy update
infraguard policy list
```

若用户只是咨询概念，可先简短回答；一旦需要生成、查询、校验或扫描策略，必须先完成懒加载检查。

## 常见流程

### 查询已有策略

1. 完成 Lazy InfraGuard Sync。
2. 使用 `infraguard policy list` 查看官方可用策略。
3. 必要时使用 `infraguard policy get <policy-id>` 查看规则详情。
4. 将策略 ID 以 `rule:aliyun:<name>` 或 `pack:aliyun:<name>` 的形式用于扫描。

### 扫描 ROS 模板

1. 完成 Lazy InfraGuard Sync。
2. 确认模板文件是 ROS YAML/JSON。
3. 使用 `infraguard scan <template.yaml> -p <policy>` 扫描；需要机器可读结果时加 `--format json`。
4. 对违规结果给出资源名、属性路径、风险原因和修复建议。

### 生成或调整自定义策略

1. 完成 Lazy InfraGuard Sync。
2. 优先查询官方策略是否已经覆盖需求。
3. 只有官方策略无法满足时才生成自定义 Rego，并保持规则聚焦在 ROS 模板可静态证明的信息上。
4. 写入用户指定文件或临时工作文件后，使用 `infraguard policy validate <policy.rego>` 校验。
5. 若用户提供模板样例，使用该策略扫描样例模板，确认命中和不命中场景。

## 策略设计原则

- 只检查 ROS 模板中可静态读取的资源、属性、引用关系和条件。
- 不把运行时指标、账单历史、审批记录、人工例外或账号侧状态写进 Rego。
- 优先复用官方策略、官方策略包和 InfraGuard 的策略更新结果。
- 自定义策略要有稳定 ID、清晰元数据、可定位的违规路径和可执行修复建议。
- 生成策略时同步给出最小违规模板和最小合规模板，便于用户验证。
