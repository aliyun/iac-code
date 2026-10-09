# E2E：本地与 CI 共用入口

`scripts/ci/run_e2e.py` 是用例选择、并发调度、超时控制和报告生成的唯一入口。它启动 `scripts/a2a/e2e/`、`scripts/repl/e2e/`、`scripts/web/e2e/`、`scripts/pipeline/e2e/` 中已有的验收脚本；已有脚本继续负责场景断言和资源清理。CI 只需检出代码、安装依赖、注入凭证、上传报告。

## 本地执行

在 iac-code 仓库根目录：

```bash
uv sync --locked --extra a2a --extra agui --group dev
uv run --no-sync python scripts/ci/run_e2e.py --suite fast --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --suite full --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --list --suite all
```

`fast` 包含 7 个 A2A、REPL、Web API 确定性契约用例；`full` 共 43 个，另含 A2A 执行控制与权限等待/恢复矩阵。`e3a-recovery` 保留运行中快照保存后的崩溃故障点，并验证用户手动继续后恢复同一任务；另设 `e3a-handoff-recovery` 验证已完成回合后的安全交接点重启，两者独立验收。Web API 用例明确使用 `--skip-browser`，不启动浏览器。只跑一个用例可用 `--case a2a-recovery-contract` 或 `--case a2a-handoff-recovery-contract`。`--jobs` 限定为 1–16，确定性套件默认 3，真实套件默认 12。每个用例用独立子进程、配置目录和日志目录；确定性用例不会继承常见 LLM 与阿里云凭证环境变量。

所有用例子进程都设置 `IAC_CODE_TELEMETRY_E2E_USER_ID`，保证遥测中的 `user.id` 带有 `e2e`，供报表排除测试流量；真实用例同时在复制后的 `settings.yml` 中保留或写入该 ID。入口清除继承的 OTLP 导出目标。仅 A2A、REPL、Web 契约及 Selling、只读 canary 等明确使用 `ObserveCapture` 验证埋点的用例设置 `IAC_CODE_TELEMETRY_LOCAL_ONLY=1`，只向本机临时接收器发送；其他用例不配置本机接收器，使用正常远端遥测。即使源码版的 `__release_date__` 为空，显式的 E2E 用户 ID 也允许这些普通用例正常上报。

真实 LLM 与云资源用例显式运行：

```bash
uv run --no-sync python scripts/ci/run_e2e.py --suite live --jobs 4 \
  --credential-source-dir /path/to/test-only-config \
  --allow-cloud-write
```

默认三文件模式下，凭证目录须含 `.credentials.yml`、`.cloud-credentials.yml`、`settings.yml`。建议使用专用测试账号、受限权限和资源配额。`live` 共 109 个：42 个 selling flow A2A/REPL 场景、8 个 A2A 只读资源选择、6 个 AGUI HTTP/SSE 资源选择、16 个 REPL 旧场景、33 个 A2A 恢复场景、3 个 VPC 模板 smoke、1 个只读云 API canary。其中包含真实 ROS 资源创建用例。需要缩小范围时可选 `live-core`、`live-recovery`、`live-multimodal`、`live-readonly`、`live-legacy`、`live-safety`、`live-repl`、`live-smoke` 或 `live-agui`，也可用 `--case` 指定单个用例。AGUI 的 6 条覆盖普通聊天与 Pipeline 的选择、取消和直接输入，通过 HTTP/SSE 运行，不需要浏览器；使用文本模型池，查询已有 VPC，不创建资源，清理状态为“无需清理”。可单独运行 `--suite live-agui`，或指定 `--case agui-selector-normal-selected`。三个 VPC smoke 只接收 LLM 与 settings 配置，使用隔离的 HOME，不接收云凭证；它们验证生成模板，不创建 VPC。并行 selling 场景各用独立的 `10.250.0.0/16` 子网池，避免独立进程重复预留同一 VSwitch CIDR；原 runner 单独运行仍沿用原池。旧 A2A、REPL 和 selling 场景不强制测试 StackName；清理读取隔离会话中真实 CreateStack 接受回执，按实际 ID、名称和地域校验后删除。仅查询、等待或模型提及的资源不能授权删除；硬超时后由独立清理进程再次尝试。selling、REPL 旧用例和 A2A 恢复用例的硬超时为 45 分钟，之后最多留 15 分钟清理，再强制结束进程组。硬超时不代表清理成功，需检查报告和测试账号残留资源。

CI 可改用 `--cloud-credential-helper /path/to/helper.py`，此时源目录只需 LLM 凭证和 `settings.yml`。Helper 接口为 `cloud --output <目标 .cloud-credentials.yml 路径>`；它在每个真实云用例开始前调用，长用例每 10 分钟调用一次，异常清理前也会再次调用。若 helper 需要独立 Python 环境，传 `--cloud-credential-python /path/to/python`。Helper 必须原子地写入私有权限文件，且不得在标准输出或错误输出打印凭证。模板 smoke 不调用 helper。本地原有的三文件运行方式继续支持。

## 多模态文字图片的字体

动态文字图片需要能覆盖图片中文字的字体。runner 优先读取 `IAC_CODE_E2E_FONT_PATH`，否则查找已安装的系统字体；缺少中文字形时直接失败，避免把缺字方块发送给模型。Linux 本地或 CI 可安装中文字体，或将该环境变量指向 [Noto Sans CJK](https://github.com/notofonts/noto-cjk) 等字体文件。固定图片继续复用已渲染的资源。

## 真实用例的模型池与思考强度

真实套件默认采用滚动调度：文本用例使用 `deepseek-v4-flash-0731`、`glm-5.2-fast-preview`、`glm-5.3-prime`、`deepseek-v4.1-flash`，每模型最多两个用例；多模态用例使用 `qwen3.8-max`、`qwen3.8-max-0902`、`qwen3.8-flash`、`qwen3.8-omni-flash`，每模型最多一个用例。两类模型池不能互相借用，总共最多 12 个用例。仅有文本用例待运行时最多八个；仅有多模态用例时最多四个。

用例完成并结束清理后立即补位。模型额度或资源锁不可用的用例在待执行队列中等待，不占 worker；五条共享清理锁的用例继续互斥。每个用例在整个运行、重启恢复和清理期间保持同一个模型，隔离配置中的 `modelFallbackEnabled: false` 禁止自动模型降级。模型的请求重试继续由产品处理，失败不会改用另一个模型掩盖结果。

默认思考强度为 `low`。Qwen Flash 用 `thinkingBudget: 2048` 控制思考，此时不传 effort，避免 effort 覆盖预算。只改每个用例的配置副本，不改传入的配置目录或本机配置；`userID` 仍须包含 `e2e`。结构化报告、HTML、Markdown 和 JUnit 都记录分配模型与思考策略。

```bash
# 同类型模型各自有额度，总并发仍受 --jobs 限制
uv run --no-sync python scripts/ci/run_e2e.py --suite live --jobs 16 \
  --text-model-jobs 3 --multimodal-model-jobs 1 \
  --credential-source-dir /path/to/test-config --allow-cloud-write

# 失败复测沿用报告中的原模型
uv run --no-sync python scripts/ci/run_e2e.py --case ssf-a2a-happy-multi-plan \
  --text-model glm-5.2-fast-preview --jobs 1 \
  --credential-source-dir /path/to/test-config --allow-cloud-write
```

`--text-model`、`--multimodal-model` 可重复使用以限制模型池。使用其他 provider 时加 `--no-model-pool` 沿用原来的模型配置。`--text-model-jobs` 和 `--multimodal-model-jobs` 可选 1–3；GLM Prime 始终最多两个测试用例，另预留一个跨进程共享的等待诊断位置。诊断位置忙时直接跳过本次建议，不等待，不影响场景判定。

这些额度限制的是同时运行的用例，不是所有内部模型请求；实际 RPM/TPM 也受同账号其他调用影响。提高并发后需观察真实限流和运行机资源，不能根据小型请求探针保证长用例吞吐。

## CI 接入

CI 调用同一个入口，并用 `--run-dir` 指定报告目录。`fast` 适合快速检查，`full` 适合完整确定性验收。真实场景必须提供测试专用配置目录和 `--allow-cloud-write`。配置文件内容应通过 CI 的 Secret 注入，且不要进入代码、作业输出或上传附件。

`report.md` 是概要表，`report.html` 可展开每个用例，`summary.json` 和各用例 `ci-result.json` 是结构化详情，`junit.xml` 供 CI 测试报告展示。确定性套件可上传 stdout/stderr；真实套件仅上传筛选后的状态文件，不上传原始日志、凭证、工作目录或场景原始 summary。

REPL 旧场景的 PTY 等待每 10 秒检查一次，每 60 秒在单用例的 stdout 日志中记录等待阶段。无终端输出时普通阶段 10 分钟、明确进入云部署/删除阶段 25 分钟后提前失败；原有 30 分钟单次等待和 45 分钟用例超时仍是兜底。单次等待达到 120 秒后，最多对两个等待阶段各调用一次百炼 `glm-5.3-prime`（最低推理强度），单次调用最多 45 秒。诊断读取该用例隔离配置中的 DashScope Key，无需额外 Secret；发送前会截短终端内容并遮盖已知 LLM/云凭证。只有模型高置信度判断需要额外输入，且终端内容同时出现明确澄清提问或候选选择控件时，才提前终止并进入原有资源清理；普通 REPL 输入提示只记录诊断，继续等待流程进展。模型调用失败不会使用例失败。在线报告仅显示固定类别、等待阶段、耗时和处理动作；原始终端内容不上传。

## 失败复盘

总入口支持 `--retries 1`：失败或超时后重新完整执行一次，第二次通过则该用例计为通过。
本地默认 `--retries 0`，CI 可显式开启一次重跑。每次执行仍使用原验收条件、原模型与思考策略，
独立配置和日志目录；重跑期间继续占用原模型额度和资源锁，不增加并发数。
任一次真实云执行的清理失败或未验证，最终仍失败，不能由下一次成功覆盖。
超时后若无法确认前次进程退出，则停止自动重跑并保留失败，避免两次云资源执行重叠。

报告用“重跑通过”区分首轮成功，耗时为两次累计，每个逻辑用例只统计一次。
首轮证据保留在原目录，第二轮在 `retry-1/`；两次分别保存安全的 `attempt-result.json`，
最终 `ci-result.json` 包含 `attempts` 与 `rerun`。Markdown、HTML 和 JUnit 日志保留每次结果、
首次失败检查和单例定向重跑参数。定向复现默认 `jobs=1`、`retries=0`，固定原模型，避免再次重跑掩盖问题。

先按 `report.md` 找到失败用例与首次失败检查，再看对应的 `ci-result.json`、场景 `summary.json`、日志和源码。Agent 应做有界复现，明确归类为产品缺陷、用例/断言缺陷、环境/凭证故障、云资源清理故障或超时，并写出证据、受影响场景和建议修复。配置的一次自动重跑之外，真实云用例再次复现前先核对残留资源与清理结果。报告里的“初步线索”只是索引，不能替代复盘结论。

总入口目前登记 152 个场景。暂未纳入的场景和原因在 `--list --suite all` 及每次报告中列出：Selling Web/Desktop、StartChat 权限等待、Qoder MCP 重连和浏览器 DOM 场景。Aone CI 没有浏览器；若未来提供浏览器运行机，再单独验证浏览器场景。真实用例在测试专用 Secret 配置后才能完成 CI 实跑验收。

### 验收边界

A2A 脱敏用例的 token 数值与一致性检查读取真实 `usage`、`context_usage` 和上下文压缩事件，
按事件 ID 对照服务端 journal 与公开 SSE。公开计数在内存中采集，不从已经脱敏的调试日志恢复，
也不把云 API 参数、响应或 schema 中同名的 `MaxTokens` 等字段当成 LLM 使用量。
原有验收标题及数值、缺失、改写检查保持不变；不一致的重复事件仍然失败。
报告仅保留真实计数的数量和旧遍历规则命中非数值字段的数量，不导出事件载荷或凭证。

R12 的两次 interrupt 回滚须在当前 REPL 中正常推进；规划停滞或等待超时直接失败，保留失败检查并执行原有资源清理，不额外强杀重启来救援通过。显式测试退出、崩溃及 `--continue` 的恢复用例仍按各自场景执行。

A01/A24 的公开工具归属审计要求存在与实际持久化调用 ID 对应的 `tool_started` 或 `tool_result`，并核对调用工具名称；空事件、错误名称和仅有 artifact 引用均失败。只公开 artifact 的契约须独立验证 artifact，不能计为工具归属检查通过。

SSF 的成功询价检查针对可用的价格，不把 `ros_estimate_template_cost` 未抛异常当作价格完整。
ROS 可能返回成功的资源 `Order`（非零 CNY 金额），却缺少 `OrderSupplement`，且结果、订单和资源属性中均无
`PriceUnit`、`PeriodUnit` 或 `Period`。这种响应无法确定月价；要求它显示“询价成功”会鼓励编造价格。
这一确定形态改由 `ROS quote without a billing period is reported unavailable` 验收：
确认载荷必须明确标记 `unavailable`、说明原因、保留非空的不可用提示，并且没有月价数字或虚构资源费用。
不可用提示必须是产品现有的本地化“询价不可用”标签；“免费”等没有数字的价格表述也不能通过。
当所有报价都属于该形态时，每次确认都必须符合上述要求；有可用报价时，原
`successful ROS quote projected into confirmation` 检查仍保留。
资源失败、无效金额、外币、响应损坏或可能含有计费周期的响应都不能用此例外绕过原检查。
此修正只影响 runner 的验收判断，不修改产品的价格投影，不限制模型、用例或资源类型。

### 真实用例的问答驱动

`scripts/e2e_question_driver.py` 根据当前持久化问题驱动 SSF 与旧 A2A/REPL 的补充澄清，
不再假定固定的提问顺序。使用同一个 DashScope Key、`glm-5.3-prime`、`low` 思考。
模型仅能选择用例提供的事实或当前问题的合法选项；回答由 runner 渲染，目标约束始终保留，
禁止生成新的资源 ID、改变目标或决定验收结果。候选选择、部署确认、取消和故障注入由场景控制。
每次调用最多 30 秒；与等待诊断共用一个 helper 槽位，最多等待槽位 15 秒，避免增加模型并发突发。
模型不可用时，自由文本问题只回退到已提供的事实；禁止自由文本且无法匹配合法选项时失败。
相同问题最多回答 3 次，每例最多 12 次；REPL 提交后最多等 20 秒确认同一问题已接收回答。

问答输入包含分字段的用例事实和最近 6 轮已准备的回答，区分新问题、补充问题和重复提问。
事实字段来自原始目标的文字片段、固定 E2E 测试用途及 runner 提供的真实参数，模型不能补造值。
回答记录单独标记是否收到持久化确认；切换目标时清空旧目标的问答记录，总次数预算不重置。
模型指出必需事实缺失时明确失败，在线报告只列固定字段名，原始问题、答案和资源参数不上传。
旧 REPL 完成阶段也处理额外澄清，但仍等待原用例要求的完成标志；故障注入和图片步骤不跳过。
等待诊断增加输入类别和对应处理器建议，runner 会核对持久化状态，并优先处理当前合法问题。
诊断建议不会自动确认部署、删除、取消、授权或判定通过；已消费的旧提问不会触发提前终止。
对于自然语言资源回答，诊断可辅助标记是否提到预期目标，原有文字检查仍独立执行，语义提示不能判通过。

必填 VpcId/ZoneId 用例由 runner 在隔离配置下只读获取真实 VPC、可用区和合法未占用网段，
分别回答当前参数问题；仍要求两个参数均被询问并回答，再到 Preview/询价和取消。
故障检查点使用真实工具成功结果或已接受的 Stack 事件；资源发现关联实际云工具调用与结果，
不递归扫描工具输出中的文档、示例和 schema。资源归属由隔离用例 session 的已接受 CreateStack ledger
和对应 pipeline attempt 证明；不向用户请求或图片注入测试 StackName，也不按名称前缀授权删除。
查询、等待和继续已有 Stack 不构成新建证明。删除时仍核对实际 Stack ID、地域以及创建记录中的真实名称。
在线报告仅保留问答计数、调用动作/参数是否符合约定、固定异常类别和源码位置，原始日志留在 worker。
