---
name: iac-aliyun-materialize-selected-candidate
description: 为用户选中的唯一方案生成并校验 ROS 模板，求解参数、PreviewStack、ROS 精确询价并请求部署确认
when_to_use: 当用户已从候选方案中选定一个方案，需要把它实现为可部署的 ROS 模板并确认部署时
user_invocable: false
---

# 实现用户选中的方案

把用户**已经选中的一个**候选方案实现为可部署的阿里云 ROS 模板：生成并校验模板、求解部署参数、PreviewStack 验证、ROS 精确询价、补齐外部必填参数，最后请用户确认是否部署。

本步骤按顺序执行两个阶段：先完成模板阶段，模板校验通过后再进入成本阶段；两个阶段结束后进入确认阶段。不要在模板还没校验通过时询价，也不要在询价前请求部署确认。

## 只实现一个方案

- 上下文中的 `solution_selection.selected_candidate` 是**唯一**要实现的方案，也是本步骤的方案事实来源。
- 不要生成第二份模板，不要重新规划架构，不要新增候选，不要为其它候选做模板、Preview 或询价。
- 不要把单个方案包装成候选数组再遍历；本步骤没有并行候选实现。
- 需要换方案或改架构时走「重新选择方案」分支，由流程回退到方案选择步骤。

## 地域

所有 API 调用都需要地域，按以下优先级确定：
1. **用户指定**（如"在北京创建"）→ 使用用户指定的地域
2. **候选与意图上下文**（`selected_candidate` 与 `solution_selection.intent.non_functional.region_preference`）
3. **工具默认地域** → 工具的 `region_id` 参数描述中会显示默认地域（如 `Defaults to 'cn-hangzhou'`），使用该默认值并告知用户
4. **均无** → 请用户指定目标地域

**注意**：ROS 的模板、资源类型、模块是全局资源，任意地域查询结果相同。不要遍历地域列表。

## 阶段 A：模板生成与校验

若 `selected_candidate.name` 精确等于 `iac-code-web-single-ecs`，先读取
`references/solutions/iac-code-web.md`，再复制 `references/solutions/iac-code-web.ros.yml`
作为模板基线；不要重新设计拓扑。

1. 分析 `selected_candidate`，确定资源列表与参数
2. 查阅 [references/cloud-products/](references/cloud-products/) 下对应产品文件，了解选型策略和库存相关属性
3. **必须**阅读 [references/ros-template.md](references/ros-template.md)，了解 ROS 模板最佳实践，未阅读不得生成模板
4. 生成 ROS YAML 模板（库存相关属性按 [references/cloud-products/](references/cloud-products/) 与 [references/template-parameters.md](references/template-parameters.md) 定义为 Parameters，所有 Parameters 必须添加 AssociationProperty），并用 `write_file` 写入 `selected_candidate.output_path`
   - 该路径相对当前工作目录；不要写入 `/tmp` 等工作目录外路径，也不要另选文件名
5. 调用 `ros_validate_template` 校验；`template_url` 必须是刚写入的同一个模板文件路径，已有具体地域时传 `region_id`，否则使用工具默认地域
6. 校验失败 → 按「模板校验只用 `ros_validate_template`」区分错误性质 → 属于模板问题时**就地修复原路径文件** → 重试（最多 5 轮）
7. 校验通过 → 进入阶段 B

模板路径是本步骤的硬约束：从模板写入、校验、Preview、询价到部署确认，全程只使用同一个文件路径，不得另写替代文件绕过错误。

> **模板路径支持本地文件**：`ros_validate_template` 的 `template_url` 可传当前工作目录内的本地文件路径（如 `./template.yml`）。避免将大模板内容直接作为参数传递。

### 模板校验只用 `ros_validate_template`

- `ros_validate_template` 用的是 ROS 感知的 YAML 解析器，`!Ref`、`!GetAtt`、`!Sub` 等 ROS 短标签都能正常解析；它是本步骤唯一的模板校验入口。
- **不要**用标准库 PyYAML 自查模板（例如在 bash 里跑 `python3 -c "import yaml; yaml.safe_load(...)"`）：`yaml.safe_load` 没有注册 ROS 短标签构造器，模板只要用了 `!Ref` 就必然抛 `ConstructorError`，这个报错与模板正确性无关，只会把排查带偏。需要查看模板内容时用 `read_file`。
- 校验报错时先区分错误性质：**模板诊断**（指向具体资源、属性、参数或行号）才修模板；**环境类错误**（登录过期、凭证或签名失败、网络不可达、准备 API 调用失败等）与模板内容无关，此时不要改模板、不要自查 YAML，按错误提示处理或如实报告失败原因，不得用重写模板的方式绕过。

### 资源生命周期约束

`selected_candidate.resource_intents` 优先级高于自然语言描述：

- `action=create` 的资源才允许出现在 ROS `Resources` 中作为新建资源。
- `action=use_existing/reference` 的资源必须建模为 Parameters 或外部引用，不得在 Resources 中创建。例如"已有 VPC 中创建安全组"时，应定义 `VpcId` Parameter，并让 SecurityGroup 的 `VpcId` 引用该参数。
- `action=forbid` 的资源不得在模板中创建；除非用户明确要求引用已有资源，也不要生成相关 Parameter。
- 候选的自然语言、products 和生命周期字段冲突时，以生命周期字段为准；冲突严重无法生成时，按「重新选择方案」分支回退到方案选择步骤。

示例：`resource_intents: [{"product": "SecurityGroup", "action": "create"}, {"product": "VPC", "action": "use_existing"}]` 时，只生成 `ALIYUN::ECS::SecurityGroup`，不要生成 `ALIYUN::ECS::VPC` 或 `ALIYUN::ECS::VSwitch`。

### 用户硬约束

`solution_selection.intent.hard_constraints` 是本步骤**唯一**的硬约束来源；候选中的兼容快照由 Python 生成：

- 模板资源数量、固定属性、Parameters、Default、AllowedValues 和 Rules 不得与任何硬约束冲突。
- 能直接表达的约束写入模板属性或参数规则；需要结合地域、库存、产品规格或已有资源才能求解的值保持参数化，在阶段 B 用产品 API 与 ROS 参数约束求解。
- 场景推荐、默认值和候选描述只能在硬约束允许的范围内选择，不得替换、升级、降级或放宽用户明确值。
- 模板结构无法满足某条硬约束时，按「重新选择方案」分支回退，不得生成一个看似成功但违反约束的模板。

### 参数化规则

库存相关属性**必须**定义为 Parameters（部署前通过 API 查询确定实际值）。具体字段按 [references/cloud-products/](references/cloud-products/) 的产品文件和 [references/template-parameters.md](references/template-parameters.md) 执行，不在本技能重复维护产品字段清单。

以下属性**不需要**参数化，直接使用合理默认值：
- 网络：VPC CIDR、VSwitch CIDR
- 命名：实例名称、资源名称
- 安全：安全组规则
- 配置：备份策略、监控设置、标签

### 资源命名

资源名称应体现业务用途，**不要**包含工具名（如 ros）：
- 好：`my-vpc`、`web-server`、`app-db`
- 差：`ros-ecs`、`ros-vpc`

### 生成要求

- 模板格式为 YAML
- 使用 `!Ref`、`!GetAtt` 等内置函数引用参数和资源属性，避免硬编码
- Outputs 中所有输出变量必须定义 Label

## 阶段 B：参数求解、Preview 与 ROS 精确询价

模板校验通过后开始成本阶段。本阶段不重复例行校验；只有在修复或改写模板后，才再次调用 `ros_validate_template`。

1. **提取参数** — 从模板 Parameters 中提取所有参数及其默认值
2. **推荐并预览验证参数** — 按下面「参数推荐与传递」完成参数求解与预览验证，不得跳过约束求解直接编造库存值
3. **补齐用户必填参数** — 按「参数缺口分类与补齐」把 `user_required` 缺口在确认之前全部收齐
4. **调用询价工具** — 优先使用 Preview-Validated Pricing Parameter Set；PreviewStack 因缺口无法通过时，可用当前已选参数调用 `ros_estimate_template_cost`
5. **按需修复模板** — 仅当询价失败且错误指向模板问题，或必须修复/改写模板时，修改模板并写回同一文件路径
6. **修改后校验并重新询价** — 调用 `ros_validate_template` 校验改动；通过后重新询价；失败则修复重试（最多 7 轮）
7. **语义输出** — 只输出新的 `solution_summary`、本轮 `parameter_overrides`、`missing_deployment_parameters` 和精简约束证据；Python 从最后一次询价输入和有序工具记录生成参数、Preview 与价格

### 按需校验模板

需要修复或改写模板的典型情况：
- 资源属性拼写错误或类型不匹配
- 缺少必要属性（如 VSwitch 缺少 CidrBlock）
- 内置函数使用不当（如 `!Ref` 引用了不存在的资源）
- Parameters 定义不完整

校验方法：
```
ros_validate_template(
    template_url="<selected_candidate.output_path>",
    region_id="cn-hangzhou",  # 已有具体地域时传；否则省略使用工具默认地域
)
```

修改后校验失败时：
1. 按「模板校验只用 `ros_validate_template`」区分错误性质；属于模板诊断时分析错误信息，定位问题资源/属性
2. 查阅 [references/](references/) 下的参考文件了解正确的属性和参数规范；如仍不确定 → 调用 `aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})` 查询 Schema
3. 修复模板并**写回同一文件路径**（部署步骤从此路径读取，不写回会导致后续步骤使用错误模板）
4. 重新校验（最多 7 轮）

### 参数推荐与传递

缺少 Default 或上下文值时，按 [references/template-parameter-recommendation.md](references/template-parameter-recommendation.md) 的参数推荐规则求解，并通过 `ros_preview_template` 形成 **Preview-Validated Pricing Parameter Set**。不要使用 `ros_stack` 执行 `PreviewStack`；本步骤只验证参数与模板可预览，不执行 `CreateStack`。

PreviewStack 必须传 StackName；调用 `ros_preview_template` 前，必须先确定唯一 `stack_name`。`stack_name` 使用候选方案或服务简名作为前缀，并追加时间或 6 位小写字母/数字随机串后缀（如 `ai-app-20260623-a1b2c3`），避免重名。该 `stack_name` 是预览工具参数，不写入模板 `parameters`，不放入 `deployment_parameters`。

本阶段的裁剪规则：

- `solution_selection.intent.hard_constraints` 是唯一硬约束来源，优先级高于候选推荐、模板 Default、场景推荐和软偏好。
- 初始 `parameter_overrides` 为 `{}`；Step 1 不接收部署参数。只有本步骤确认交互或参数补齐问答得到的用户值才作为最高优先级覆盖，与其它推荐冲突时以这些 Step 2 用户值为准。
- 优先使用上下文已有值和模板 Default；库存相关参数缺值时，先通过 `ros_get_template_parameter_constraints` 获取合法 `AllowedValues`，必要时再按 [references/cloud-products/](references/cloud-products/) 的可用性 API 与选型策略补足。
- 每条硬约束只提交 `constraint_id`、LLM 独立判断的 `status`、语义 `actual_value/actual_unit`、相关最终 `parameter_values` 和可用的 evidence locator，不复制 constraint。
- `type: tool` 证据提交真实 ordered record 的 `record_id` 与 `result_path`，可带 `tool_name`；`type: context` 提交受限 `context_path`；`type: template` 提交 `template_path` 或最终参数 `parameter_name` 二选一。这里的 `template_path` 是最终 ROS YAML **内部字段的点路径**（例如 `Resources.VSwitch.Properties.CidrBlock`），绝不是模板文件路径；模板参数值优先用 `parameter_name`。不要提交 evidence summary 或 evidence actual，Python 会从权威来源读取。
- 工具已真实尝试但响应没有可定位的结果字段时，`evidence` 提交空数组，不得编造 `record_id`、`result_path` 或工具返回值。Python 会核对可用 locator、实际值、参数子集、operator/value/unit，并生成完整公共 hard_constraint_checks。接受规则保持兼容：LLM `status=satisfied` 或 Python code verification 通过任一成立即放行；只有二者都不通过才阻止确认。
- VpcId、VSwitchId、SecurityGroupId、KeyPairName 等已有资源参数：先查询约束或只读资源候选；API 返回候选不是编造，可作为参数候选参与回溯与 PreviewStack。没有上下文值、模板 Default、用户提供值或 API 返回候选时，才按外部输入缺失处理。
- 只能在合法候选内筛选或排序，不得编造 API 未返回的库存值；LicenseKey、Token、证书、真实域名等外部输入不得编造。不要仅因参数名是 VpcId、VSwitchId、SecurityGroupId 或 KeyPairName 就跳过参数推荐并直接停止询价。
- 对可生成参数要主动补齐：普通密码等应生成合规随机值，并让同一真实值贯穿 Preview 与最后一次询价的 `parameters`；Python 直接以该询价 input 作为最终参数锚点。不得写入占位值。
- `PreviewStack` 因候选组合不可行失败时，按 reference 的回溯规则更换候选值；因外部输入缺失失败时，记录缺口，不用占位值伪造。
- 最终参数集不写入模板 `Default`；模板 Default 只是参数求解来源。跨步骤参数由 Python 从最后一次 `ros_estimate_template_cost.input.parameters` 投影。
- PreviewStack 成功但询价失败时仍使用同一参数调用询价；失败记录的 input 也能建立 ParameterSetAnchor，Python 会投影失败状态。

### 参数缺口分类与补齐

把仍未解出的参数写入 `missing_deployment_parameters`，并逐项标注 `classification`：

- `auto_solvable`：可继续用 `ros_get_template_parameter_constraints`、产品只读 API 或规则生成解出的参数（库存规格、可用区、普通密码、名称、CIDR 等）。这类缺口应尽量在本步骤解掉，不要过早列入缺口。
- `user_required`：只能由用户提供的外部输入（已有资源 ID、KeyPairName、LicenseKey、Token、证书、真实域名、第三方账号等）。这类缺口同时写入 `user_required_missing_parameters`。

**部署确认之前必须补齐所有 `user_required` 参数**：用 `ask_user_question` 一次只问一个参数，允许自由输入，说明参数用途和格式要求。收齐后 `user_required_missing_parameters` 必须是空数组，否则不得提交 `status: confirmed`。

### 调用询价 API

通过 `template_url` 传递模板文件路径（不要用 `TemplateBody` 内联模板内容，模板可能很大）。`parameters` 直接传字典格式；不要手动展开：

```python
ros_estimate_template_cost(
    template_url="templates/1-simple-ecs.yml",
    parameters={
        "ZoneId": "cn-hangzhou-k",
        "InstanceType": "ecs.g7.large",
        "ImageId": "ubuntu_24_04_x64_20G_alibase_20260720.vhd",
        "SystemDiskCategory": "cloud_essd",
    },
    region_id="cn-hangzhou",
)
```

参数值来源：
- `hard_constraints` 中用户明确指定的规格/参数 → 按通用 operator、value、unit 做不可放宽的约束求解
- 本步骤确认交互和参数补齐问答得到的用户值 → 最高优先级；Step 1 不接收部署参数覆盖
- 上下文中已有可用性选择结果且不违反硬约束的 → 使用上下文值
- 模板 Parameters 中有 Default 值且上下文未覆盖的 → 使用默认值
- 没有 Default 的库存相关参数（ZoneId、InstanceType 等）→ 按「参数推荐与传递」求解，不要直接编造
- PreviewStack 成功时，用于询价的参数集必须与 PreviewStack 验证通过的参数集一致

### 价格口径

本阶段的价格是 **ROS 询价**结果，展示时必须标注「ROS 询价」，不得使用方案选择步骤的「架构粗估」区间作为精确价格，也不得在询价失败时回退使用粗估价。

价格字段不由模型复制。Python 从 ParameterSetAnchor 的真实响应读取 `OriginalAmount`（列表价）、
`TradeAmount`（合同优惠价）、`Currency` 和 `Resources`，按
`¥<OriginalAmount>/月（列表价，合同优惠后约 ¥<TradeAmount>/月）` 投影到公共 cost 路径；明确空
`Resources` 且无金额才是 `¥0/月`，缺失或无效字段不得冒充免费。

### Preview 软门槛

模型不提交 `preview_validation`。Python 只接受路径、parameters 和有效 region 与 ParameterSetAnchor 全等的最后一次 Preview 记录。

Preview 失败**不禁止**确认部署：只要模板已校验通过、`user_required` 参数已补齐，并且 Preview/询价失败原因已如实展示，用户仍可确认，此时 `preview_ready_for_create: false`，由部署步骤走既有校验路径。

`preview_ready_for_create` 完全由 Python 根据匹配 Preview 和参数缺口计算，模型不要提交。

### ROS 模板修复参考

| 文件 | 内容 | 何时查阅 |
|------|------|----------|
| [references/cloud-products/](references/cloud-products/) | 云产品选型文件（ecs.md、rds.md、redis.md、slb.md、vpc.md、oss.md） | 需要了解产品属性、规格选型、库存相关字段时 |
| [references/template-parameters.md](references/template-parameters.md) | 模板参数规范：AssociationProperty、Label、分组 | 生成或修复 Parameters 定义时 |
| [references/ros-template.md](references/ros-template.md) | ROS 模板最佳实践：RunCommand、嵌套栈、条件部署 | 生成或修复资源定义、内置函数用法等模板结构问题时 |
| [references/template-parameter-recommendation.md](references/template-parameter-recommendation.md) | 参数推荐与回溯规则、PreviewStack 参数集形成方法 | 求解库存/已有资源参数并形成预览参数集时 |
| [references/solutions/](references/solutions/) | 预定义方案基线（如 iac-code-web） | `selected_candidate.name` 命中预定义方案时 |

## 阶段 C：部署确认

模板和询价结果就绪、`user_required` 参数补齐后，生成与当前最终参数一致、面向最终用户的 `solution_summary`。摘要只说明产品组合和拓扑、地域、主要规格、资源数量和新建/复用关系；必要时用一句话说明影响用户决策的重要假设或风险。通常控制在 2～5 句，不得写模板路径、StackName、PreviewStack/校验状态、参数 JSON、内部资源类型或 API 名称，也不要重复总价和价格明细——确认界面会从 `cost` 单独展示询价概览和费用明细。`cost.resources[].type` 使用用户可理解的产品或资源名称，不使用 `ALIYUN::...` 资源类型。参数变化后必须重新生成，不能复用 Step 1 的粗略摘要。

最终确认使用 pipeline 专用的 `deployment_confirmation` 等待态，**不得调用 `ask_user_question` 代替最终确认**。调用 `complete_step` 只提交 `status: awaiting_confirmation`、`solution_summary`、`parameter_overrides`、参数缺口和精简硬约束证据；Python 生成模板元数据、价格、Preview、最终参数、提示和动作选项：

- `confirm`：确认部署
- `cancel`：取消
- `reselect`：仅当 `solution_selection.candidates` 多于 1 个时展示，用于重新选择方案

不要把 `adjust` 放入可见选项。参数调整、架构变化和全新部署意图统一由用户直接输入自然语言；底层仍接受 Web、Desktop、A2A 等调用方提交结构化 `action: adjust`。

流程布尔字段由 Python 生成。Web、Desktop、A2A 可以提交结构化 JSON；用户也可以直接输入自然语言。

提交等待态前不要再用普通助手文本重复方案、价格、Preview 或参数；确认界面会从结构化 conclusion 统一渲染。

### 恢复输入判定

- 能解析为 `{"action":"...","parameter_overrides":{...}}` 的结构化输入必须严格按 action 执行，不得由模型改判。
- 当前 `selected_plan.status` 已是 `awaiting_confirmation` 时，结构化 `confirm` 就是最终授权：无论是否携带参数覆盖，
  都直接沿用当前模板提交 `confirmed`；不得重做模板、Preview、询价，也不得再次提交 `awaiting_confirmation`。
  界面已用「模板正文 + 最新参数」自行询价，Python 负责把旧询价参数与本轮覆盖合并成最终参数并校验合法性。
- 非结构化输入由 LLM 像旧 pipeline 的确认步骤一样判断为确认、取消、调整当前参数、重新规划当前架构或替换为全新部署意图，并提取用户明确给出的参数值。参数调整留在本步骤重算；架构变化或全新部署意图回滚 Step 1，且全新意图以最新输入替换旧部署目标，不能合并新旧需求。
- 自然语言含义不清或缺少具体参数值时，可以用 `ask_user_question` 澄清一个缺口；工具回答只用于澄清，不能直接作为最终部署授权，处理完成后仍须回到专用等待态。
- 空的 `parameter_overrides` 表示没有用户覆盖，是合法状态。

### 模型增量与 Python 权威投影

首次 awaiting 提交模型 schema 要求的五类语义字段。确认只提交 `status: confirmed`；取消只提交
`status: cancelled`；重新规划提交 status 和 reselect_reason。Python 将原始用户输入绑定为 confirmation/cancellation，
并为 reselect 生成固定 outer rollback_request。用户自然语言要求调整参数时，必须形成新的询价 anchor、Preview 与
solution_summary，再提交新一轮 awaiting 语义字段，不能沿用旧事实。

提交确认结论前不得再次写模板、再次校验、再次查询参数约束、再次 Preview 或再次询价；这些操作发生在确认之后会使确认失效，必须重新向用户确认。

### 调整参数

结构化 `action: "adjust"` 或自然语言明确要求调整时，在当前 AgentLoop 内合并用户明确给出的最新覆盖值，重新执行必要的参数约束查询、PreviewStack 和 ROS 精确询价，按新参数、资源、价格和风险重新生成 `solution_summary`，然后再次提交 `status: "awaiting_confirmation"`。参数修改不得静默改变用户硬约束；与硬约束冲突时告知冲突并要求用户明确修改原要求。

结构化 `action: "confirm"` 携带与当前值不同的参数覆盖时**不是**调整请求：这是一次明确授权，Python 直接确定性地合并参数并进入部署，不重算、不重新询价、不再次等待确认。只有 `adjust` 或没有确认语义的自然语言参数修改才走上面的重算路径。

### 重新选择方案、修改架构或改变部署意图

调用 `complete_step` 提交 `status: reselect_requested` 和完整 `reselect_reason`。Python 固定生成回到 `solution_planning_and_selection` 的 outer rollback_request。不要自行替换产品组合、创建新候选或继续修改旧模板。

### 取消

调用 `complete_step` 只提交 `status: cancelled`；Python 从本轮原始输入生成 cancellation_reason。

### 选择无效

`solution_selection.selected_candidate` 缺失或不一致时不要生成模板：提交 `status: reselect_requested` 和真实原因，Python 负责回滚。

## 资源和文档搜索

- 不确定的资源属性或 Schema → `aliyun_api(product="ros", action="GetResourceType", params={"ResourceType": "<类型>"})`
- 不熟悉的资源类型/属性 → `aliyun_doc_search`（category_id=28850）
- 摘要不够 → `web_fetch` 获取完整文档

## 重要约束

- **必须**使用 `ros_get_template_parameter_constraints`、`ros_preview_template`、`ros_estimate_template_cost`、`ros_validate_template` 处理 ROS 模板参数约束、预览、询价和校验；不要直接调用 `aliyun_api` 的对应 ROS 模板 API，也不要传 `TemplateBody`、`TemplateId` 或 `TemplateScratchId`
- **不要**创建、更新或删除任何云资源；本步骤只做只读校验、Preview 和询价
- **不要**使用 `ros_stack` 或 `ros_stack_instances`；允许使用 bash 辅助本地模板生成和检查，但不得借此创建、更新或删除云资源，也不得用 bash 里的标准库 PyYAML 代替 `ros_validate_template` 校验模板
- **不要**搜索定价文档或使用 `aliyun_doc_search` 查询价格
- 询价失败时报告错误原因，不要编造费用数据
- 修复模板后**必须写回同一文件路径** — 部署步骤直接使用此文件，未写回等于向下游传递错误模板
- 修改后校验不通过时**不要跳过修复直接询价**，错误模板会导致后续部署失败

## 输出

调用 `complete_step` 提交 tool schema 中的模型字段。不要提交 candidate、模板正文/路径/地域、价格、Preview、
deployment/effective parameters、confirmation、UI options 或流程布尔字段；这些都由 Python 从权威上下文、文件和
ordered tool records 生成。没有硬约束时 `hard_constraint_checks` 填 `[]`，没有缺口时
`missing_deployment_parameters` 填 `[]`。
