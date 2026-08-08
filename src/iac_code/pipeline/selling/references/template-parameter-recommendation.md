# 已有模板参数推荐

用户已有 ROS 可部署模板，要求在 `CreateStack` 前推荐、补全或预览参数时使用本流程。

## 适用范围

- 支持：ROS 原生模板；ROS Terraform 类型模板（`Transform: Aliyun::Terraform-*` / `Aliyun::OpenTofu-*`）。
- 不支持：纯 Terraform 工作目录（先按 [terraform-template.md](terraform-template.md) 用 `tf2ros.py` 打包）；`UpdateStack`、`ContinueCreateStack`、栈组、栈实例。

## 核心原则

- 仅推荐 `CreateStack` 前的新建栈参数，由 skill 编排专用 ROS 模板工具和必要的只读资源查询。
- `PreviewStack` 是成本估算前必须先尝试的预览验证步骤，但不是成本估算硬门禁，也不是部署成功保证。通过的参数集称为 **Preview-Validated Parameter Set**：表示指定模板来源在当时参数集下可预览。
- `hard_constraints` 中用户明确给出的等值、范围、枚举和禁止项是硬约束，不是推荐偏好；模板 Default、场景推荐和“更稳妥”的替代都不得覆盖或放宽它们。`verification_mode: direct` 由模板/参数直接证明，`verification_mode: tool` 必须用产品 reference 指定的查询结果证明。
- 原始约束快照与 API 响应只在 agent 上下文持有，不写额外文件；结构化步骤交接和用户结果保留完成任务所需的真实值。
- 密码类参数可在用户要求时生成合规随机值；同一个真实值必须贯穿预览、询价、结构化步骤交接和部署，不得用脱敏占位符替换。
- 不编造外部资源、账号资源、AccessKey/Secret/Token/Webhook/LicenseKey/证书/真实域名/已有资源 ID。
- 服务端日志由运行时执行严格脱敏；不得为了日志策略改写功能状态或用户结果。

## 流程

### 1. 确认输入

| 项 | 来源 |
|---|---|
| 模板 | 本地/远程模板路径或对话中的产物；调用专用工具时传给 `template_url` |
| 地域 | 用户指定 → 工具默认地域 → 询问用户 |
| `StackName` | 用户指定基础名 → 由模板描述/文件名生成基础名；调用时追加唯一后缀 |
| 共享操作参数 | 至少 `DisableRollback: true` |

本流程输出的预览证明只用于说明模板来源已通过预览；后续选择或部署阶段可以调整部署参数，最终参数由 `CreateStack` 校验。

### 2. 读取模板

ROS 原生：`Parameters`（类型/描述/Label/Default/NoEcho/AllowedValues/AssociationProperty）、`Resources` 中的参数引用、`Metadata` 分组、`Outputs`。

ROS Terraform 类型：顶层 `Transform` / `Workspace`、`.tf` 的 `variable`、variable `description` 中的 AssociationProperty/Metadata/Label JSON、`.metadata` 分组、resource/data source 引用。注意 data source 索引风险（`data.*.ids.0`、`zones[0]`、`images.0.id`）：若过滤条件硬编码且未参数化，预览失败属于模板问题。

输出内部参数清单：参数名、含义、必填、敏感、默认值、引用位置、来源分类（API 约束 / 可推断 / 可生成测试值 / 外部输入 / 用户必须确认的已有资源）。

### 3. 提取用户偏好

先把用户明确给出的等值、上限和下限规格提取为硬约束，再提取成本、高可用等软偏好。硬约束必须精确参与候选求交；软偏好只用于对满足硬约束的候选排序。

| 关键词 | 倾向 |
|---|---|
| 低成本 / 测试 / 便宜点 | 小规格、按量付费、低档磁盘、减少可选付费资源 |
| 生产 / 稳定 / 高可用 | 避免过小规格，多可用区 |
| 已有资源 | 优先 API 返回或用户提供的已有 VPC/VSwitch/SG/Bucket/KeyPair |
| 安全 / 不要公网 | 私网、私有权限、避免公网暴露 |

偏好只能在同时满足用户硬约束和 API 硬约束的合法候选内删减或排序，不能覆盖任何硬约束。

### 4. 调用 ros_get_template_parameter_constraints

```python
ros_get_template_parameter_constraints(
    template_url="/absolute/path/to/template.yml",
    # 后续可带已选参数继续求解
    parameters={"ZoneId": "cn-hangzhou-h", "InstanceType": "ecs.e-c1m1.large"},
    # 已有具体地域时传 region_id；否则使用工具默认地域
    region_id="cn-hangzhou",
)
```

`parameters` 使用字典；不要手动展开成 `Parameters.N.ParameterKey`，也不要用 `ParametersOrder.N` 求解（仅控制台填写顺序提示）。

ROS Terraform 类型模板若返回 `TerraformStackNotSupported` / `QueryErrors` / 无可用约束：解析 variable AssociationProperty 与 Metadata → 调用产品可用性 API 或资源类型定义 → 按偏好生成候选 → 用 `ros_preview_template` 兜底。失败若来自 data source 固定筛选或空列表，按模板问题报告。

### 5. 保留原始约束快照（仅上下文）

字段：参数名、原始 `AllowedValues`、`AssociationParameterNames`、`Behavior` / `BehaviorReason`、`IllegalValueByParameterConstraints`、`IllegalValueByRules`、`NotSupportResources`、`QueryErrors`，以及本地模板中的引用证据。不要向结构化结果注入 `***`、`[REDACTED]` 或 `<redacted>`。

### 6. 对 AllowedValues 做偏好预筛选

- 先按 `hard_constraints` 的 `operator/value/unit` 筛选。模板参数值不能直接证明实际产品属性时，按对应 [cloud-products/](cloud-products/) reference 调用产品 API 获取实际值，不得根据资源规格名或经验猜测。
- 只能删除或排序 API 返回的值，不能创造新值。
- 保留足够候选用于回溯；记录每个被删值的原因。
- 仅软偏好预筛为空时才可恢复原始候选并标记偏好冲突；用户硬约束筛选为空时不得恢复不匹配候选，必须报告参数冲突。

### 7. 联动求解与回溯

可逆搜索：

1. 优先选择被依赖、候选少、出现在 `AssociationParameterNames` 中的参数。
2. 选当前最高优先级候选 → 带部分参数再次调 `ros_get_template_parameter_constraints`。
3. 新返回的 `AllowedValues` 与剩余候选求交。
4. 任一必填参数无候选 → 记录原因并回退。
5. 直到所有可自动推荐参数有值，或遇到不可代填的外部输入。

内部状态（不写文件）：原始快照、当前预筛候选、当前组合、已尝试值、被拒原因、最新约束响应。

### 8. 处理无 AllowedValues 的参数

读取 `Behavior` / `BehaviorReason` / `NotSupportResources` / `QueryErrors`，定位模板引用（`Ref`、`${Param}`、嵌套属性、Terraform 引用），必要时查资源属性定义（ROS `GetResourceType` / IaCService 的 Terraform 资源类型定义）。按下表分类：

| 类别 | 示例 |
|---|---|
| 可推断配置 | 本模板创建资源的名称、CIDR、布尔、小数值、非敏感字符串、模板支持的安全默认值 |
| 可生成测试输入 | ECS/RDS/Redis/RocketMQ/WordPress 等普通密码（参数名/`NoEcho`/AssociationProperty/描述/资源属性表明是密码，且用户要求代理准备） |
| 外部 / 账号特定 ❌ 不得编造 | `VpcId`、`VSwitchId`、`SecurityGroupId`、`KeyPairName`、已有 `InstanceId`；真实域名/ICP/DNS/证书；Token/Webhook/AK/SK/LicenseKey/ARMS/MSE 等凭证 |

生成的密码必须满足模板长度、复杂度、`AllowedPattern`、`ConstraintDescription`，并以同一个真实值用于 Preview、询价与后续部署；服务端日志脱敏由运行时负责。

已有资源参数：只读查询可在用户确认后校验；模板会修改资源、DNS、安全组或 RunCommand 的，必须显式确认资源与影响，不自动选择账号内资源。

### 9. ros_preview_template 预览验证

```python
ros_preview_template(
    template_url="/absolute/path/to/template.yml",
    stack_name="final-intended-stack-name",
    parameters={"ZoneId": "cn-hangzhou-h", "InstanceType": "ecs.e-c1m1.large"},
    # 已有具体地域时传 region_id；否则使用工具默认地域
    region_id="cn-hangzhou",
)
```

- 预览成功证明记录本次 `template_url` 与预览时使用的参数集；后续选择或部署阶段调整参数时，不需要因此重做 `PreviewStack`。
- 成功 → 形成 Preview-Validated Parameter Set。
- 失败 → 记录组合与错误，回到第 6/7 步。
- 最多尝试 5 个组合；5 次失败后停止，总结冲突并询问用户。
- 失败来自外部输入缺失 → 停止并要求用户提供，不用占位值伪造。
- 失败来自模板固定配置（不存在的条件名、data source 空列表、硬编码规格不可用）→ 按模板问题报告。

### 10. 可选询价

`PreviewStack` 成功后，用户准备部署或询问成本时调用 `ros_estimate_template_cost`。询价失败不丢弃 Preview-Validated Parameter Set，仅展示原因。

### 11. 展示结果

- 模板来源、地域、最终 `StackName`、共享操作参数（如 `DisableRollback`）。
- 推荐参数值；每个参数的来源（API `AllowedValues` / 可推断 / 生成的测试密码 / 用户提供 / 外部未提供）与理由。结构化参数必须保留真实值。
- `PreviewStack` 状态、资源摘要。
- 外部输入缺口与用户需提供内容。
- 是否执行 `CreateStack` 的确认问题。

### 12. CreateStack

同时满足才能进入写操作：

- 已形成 Preview-Validated Parameter Set，且没有完整部署参数缺口。
- 用户确认；高成本、多地域、修改已有资源、RunCommand、DNS/证书/域名操作单独说明影响。
- 部署时使用最终确认的模板来源和部署参数；最终参数由 `CreateStack` 校验。

部署后注意：

- 报告和结构化结果保留授权用户完成使用所需的真实参数；不得把 NoEcho/密码替换成脱敏占位符。服务端日志仍由运行时严格脱敏。
- 不要把 `CREATE_COMPLETE` 当作任务结束信号；按用户意图判断是否需要清理或后续验证。

## 失败分类

| 类别 | 含义 |
|---|---|
| 参数不可行 | 候选被约束拒绝、依赖组合无交集、Preview 回溯耗尽 |
| 模板问题 | 语法/条件名错误、Terraform data source 空列表、硬编码规格/地域不可用、Output 引用错误 |
| 外部输入缺失 | LicenseKey、Token、证书、真实域名、已有资源 ID 等不可代填 |
| 供应商/API/库存异常 | RDS `InternalError`、定价计划缺失、临时 API 错误 |
| 应用初始化失败 | `ALIYUN::ECS::RunCommand`、SAE 部署任务、WaitCondition 超时 |
| 清理失败 | 删除超时、残留安全组/ServerGroup/依赖资源，需补充清理与最终确认 |

Preview 成功但 Create 失败时，先判断失败是否来自 provider/API、模板资源关系、应用脚本或清理流程，不要直接否定推荐参数。
