# Provider 模型更新维护指南

本文用于维护 Python 版 `iac-code` 的 LLM provider。一次模型升级通常不只是把新模型 ID
加入列表，还会影响默认模型、能力标记、思考参数、请求协议、降级链、遥测和测试。

本文默认只修改 `src/iac_code/` 和 `tests/`。除非任务明确要求，**不要修改 `iac-code-rs/`**。

## 核心原则

1. 只把官方模型目录、API 文档、迁移/弃用公告作为事实依据。发布博客可以用于发现线索，
   不能单独证明 API 模型 ID、参数或可用区域。
2. 记录精确的 API model ID，不要把产品名、网页标题、控制台显示名当作 model ID。
3. “OpenAI compatible”只代表基础接口相似，不代表思考、缓存、工具调用等扩展参数相同。
4. 同一个模型通过官方端点、百炼、Token Plan 或其他代理调用时，要分别调研和建模。
5. 新模型进入默认位之前，要确认可用性、稳定性、能力和降级路径；preview 模型不能因为版本号
   更新就自动成为所有 provider 的默认模型。
6. 测试不得调用真实 LLM、真实账号或本地用户配置。真实 API smoke test 只能作为人工补充，
   不能进入 pytest。
7. Provider 返回的签名、加密思考块等 opaque metadata 不是展示内容，但可能是下一轮请求的必填字段；
   必须原样持久化、按 provider 隔离回传，并从公开输出和日志中排除。

## 标准流程

### 1. 确认范围和当前基线

先确认本次要更新哪些 provider，以及是否包含它们的区域版、套餐版和兼容版。例如：

- OpenAI 与 Azure OpenAI 是两个可用性边界，不应默认完全同步。
- Kimi CN、Kimi Intl 可以共享实现，但 model ID 和上线时间仍需分别确认。
- DashScope 标准兼容端点、Token Plan、Coding Plan 的模型集合可能不同。
- 官方 GLM/Kimi/DeepSeek 与百炼托管的同名模型可能使用不同请求参数。

开始前执行：

```bash
git status --short --branch
git log -5 --oneline -- src/iac_code/providers
```

如果工作区已有他人修改，保留并绕开它们。需要了解最近一次大规模模型更新时，可查看：

```bash
PROVIDER_UPDATE_COMMIT="$(git log -1 --format=%H -- src/iac_code/providers tests/providers)"
git show --stat "$PROVIDER_UPDATE_COMMIT"
git show "$PROVIDER_UPDATE_COMMIT" -- src/iac_code/providers tests/providers
```

### 2. 建立官方信息源

优先顺序如下：

1. 模型目录或模型总览：确认精确 ID、状态、模态、上下文和输出限制。
2. API reference：确认 endpoint、请求字段、响应字段、流式事件和错误约束。
3. 思考/推理专题文档：确认开关、effort、budget、always-on 行为及默认值。
4. 迁移与弃用公告：确认别名、快照、替代模型和下线日期。
5. 官方 models/list API 或控制台：用于确认账号、区域或套餐的实际可见性。
6. 官方发布日志或博客：只作为交叉验证和发现入口。

常用官方入口：

| Provider | 模型目录 | 参数与迁移重点 |
| --- | --- | --- |
| OpenAI | [Models](https://developers.openai.com/api/docs/models) | [Reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Deprecations](https://developers.openai.com/api/docs/deprecations) |
| Anthropic | [Models overview](https://platform.claude.com/docs/en/about-claude/models/overview) | [Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) |
| Gemini | [Models](https://ai.google.dev/gemini-api/docs/models) | [Thinking](https://ai.google.dev/gemini-api/docs/thinking)、[OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai) |
| Qwen / DashScope | [模型列表](https://help.aliyun.com/zh/model-studio/models) | [OpenAI 兼容](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking) |
| Kimi | [Model List](https://platform.kimi.ai/docs/models) | [Model parameters](https://platform.kimi.ai/docs/api/models-overview)、[Chat Completion](https://platform.kimi.ai/docs/api/chat) |
| GLM / Z.AI | [Chat Completion](https://docs.z.ai/api-reference/llm/chat-completion) | 在同一 API reference 中核对 `thinking`、工具调用和模型枚举 |
| Azure OpenAI | [Create and deploy](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/create-resource?view=foundry-classic) | 请求中的 `model` 是用户自定义 deployment name，不是公开模型 ID |
| DeepSeek | [Models and pricing](https://api-docs.deepseek.com/quick_start/pricing) | 核对官方 Chat Completions、思考模式和精确模型 ID |
| MiniMax 官方 | [Models](https://platform.minimax.io/docs/guides/models-intro)、[API Overview](https://platform.minimax.io/docs/api-reference/api-overview) | 中国/国际直连端点、Anthropic/OpenAI 兼容协议和套餐模型分别核对 |
| DashScope 托管 MiniMax | [DashScope MiniMax API](https://help.aliyun.com/en/model-studio/minimax-api-by-minimax) | 不要把官方端点的模型命名和 thinking 协议直接套到百炼命名空间 |

官方页面可能改变路径。链接失效时，从厂商文档首页重新搜索，不要转而引用聚合站、新闻稿或
模型排行榜。查询时优先使用下面这类窄关键词：

同一厂商的概览页与端点参数页发生冲突时，优先采用与当前 adapter 实际调用方式一致的端点级 API
reference，并在证据附录记录冲突。例如百炼“深度思考”概览曾遗漏 `qwen3.6-flash`，但 OpenAI
兼容 Chat 参数页明确将它列入 `preserve_thinking` 支持范围，当前 adapter 应遵循后者。

```text
site:官方域名 models API exact model id
site:官方域名 reasoning effort thinking budget
site:官方域名 deprecation migration model
```

### 3. 填写证据表，再开始改代码

每个“provider + model”至少记录下表字段。来源列应链接到具体官方页面，并写明核验日期。

| 字段 | 要确认的内容 |
| --- | --- |
| Provider key | 仓库中的 key，例如 `openai`、`dashscope_token_plan`、`kimi_cn` |
| 精确 model ID | 大小写、连字符、日期后缀、命名空间前缀是否完全一致 |
| 生命周期 | GA、preview、latest alias、dated snapshot、deprecated、下线日期 |
| 可用范围 | 中国/国际区域、标准计费/Token Plan/Coding Plan、账号白名单 |
| API 协议 | 原生 OpenAI、原生 Anthropic、OpenAI-compatible 或厂商扩展 |
| 输入输出模态 | 文本、图片、音频、视频；注意“模型支持”与“当前 endpoint 支持”之别 |
| Agent 能力 | tool calling、parallel tools、structured output、system message |
| 容量 | context window、最大输出、思考 token 是否计入输出上限 |
| 思考控制 | 开关字段、effort 枚举、budget、默认值、是否 always-on |
| Opaque 响应字段 | thinking signature、redacted/encrypted block、tool-call signature 是否必须在下一轮原样回传 |
| 缓存 | 隐式/显式缓存、cache marker、支持模型列表 |
| 降级候选 | 同协议、同模态、仍在线、成本和质量可接受的模型 |
| 证据 | 官方 URL、页面标题、核验日期；有冲突时记录冲突和采用理由 |

建议在 PR 描述或临时调研笔记中使用以下模板：

```markdown
| provider_key | model_id | status | scope | multimodal | context | max output | thinking wire format | fallback | source | checked_at |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| openai | example-model | GA | global | image input | 128K | 16K | reasoning_effort=... | example-small | official URL | YYYY-MM-DD |
```

不要仅凭 `GET /models` 决定能力。模型列表通常不能完整表达思考档位、多模态限制、弃用日期或
代理端点的扩展参数；它只能作为官方文档的补充证据。

## 代码更新矩阵

### 模型目录

修改 [`src/iac_code/providers/registry.py`](../src/iac_code/providers/registry.py)：

- 增删 `ModelEntry`，保持官方 model ID 原样。
- 每个非空 provider 最多一个 `is_default=True`。
- 仅在当前接入路径确实支持图片输入时设置 `support_multimodal=True`。
- 检查 `base_url`、`provider_class`、`qwenpaw_provider_ids` 和 `qwenpaw_chat_model`。
- 模型顺序通常为推荐默认、当前主力、较旧兼容模型；不要把已弃用模型放到默认位。

新增 provider 或全新模型前缀时，还要检查
[`src/iac_code/config.py`](../src/iac_code/config.py) 中的 `_MODEL_PREFIX_TO_PROVIDER`。如果模型名带
命名空间，或同一前缀在不同 endpoint 上有重名模型，还必须在 `_MODEL_EXACT_TO_PROVIDER` 中登记精确
映射；例如 Token Plan 的 `qwen3.8-max-preview` 不能被通用 `qwen` 前缀误判到标准百炼端点。

### 上下文与输出容量

官方 context window 或最大输出发生变化时，检查
[`src/iac_code/services/context_manager.py`](../src/iac_code/services/context_manager.py)：

- 只有同一前缀下所有模型容量一致时才复用前缀配置；endpoint 特有模型使用精确 model ID 配置。
- context window 影响自动压缩阈值，不能只记录在调研表或 registry 注释中。
- 最大输出没有精确官方证据时保持保守值，并在证据附录说明，不要从 context window 反推。
- 同一模型在官方、代理或命名空间端点下可能使用不同 ID；默认模型、alias 和 endpoint 变体都要覆盖。
- provider 会持久化的可见 thinking、signature 和 encrypted block 必须进入 token 估算，否则长工具循环会延迟压缩。
- 在 `tests/services/test_context_manager.py` 断言精确容量，并把该测试加入聚焦命令。

### 思考能力声明

修改 [`src/iac_code/providers/thinking.py`](../src/iac_code/providers/thinking.py)：

- 按 `(provider_key, model_id)` 登记，不要只按 model ID 登记。
- 确认 `ThinkingFamily`、允许的 effort、默认 effort、budget 和输出 token 策略。
- 仅在 CN/Intl 或套餐版协议完全一致时使用 `_THINKING_FALLBACK` 复用。
- 未确认的组合应保持 `ThinkingFamily.NONE`，不要猜测“新版本应该兼容旧参数”。

特别注意：能力声明只描述“支持什么”，实际请求格式由 provider adapter 组装。

### 请求协议与响应解析

根据证据修改对应 adapter：

| 协议变化 | 常见文件 |
| --- | --- |
| OpenAI `reasoning_effort`、completion token | `openai_provider.py` |
| Anthropic adaptive thinking、legacy budget | `anthropic_provider.py` |
| DashScope `enable_thinking`、budget、显式缓存 | `dashscope_provider.py` |
| Kimi 分版本 thinking 行为 | `kimi_provider.py` |
| Gemini OpenAI-compatible 扩展 | `gemini_provider.py` |
| GLM `thinking.type` | `zhipu_provider.py` |
| DeepSeek 官方扩展字段 | `deepseek_provider.py` |

至少检查这些行为：

- 未配置思考参数时是否应该省略字段，而不是擅自开启或关闭。
- `thinking_enabled=False` 对 always-on 模型应如何处理。
- 非法 effort 是忽略、回退默认值，还是应在上层拒绝。
- `max_completion_tokens` 是否需要包含思考 budget。
- 流式和非流式是否都能解析 thinking/reasoning content。
- 带工具调用的历史 assistant message 是否需要回传 reasoning content。
- 回传历史 reasoning 是否还要求 `preserve_thinking` 一类请求开关；该开关必须按官方支持列表发送，
  不能从“模型支持 thinking”直接推导。
- 流式 delta 中是否另有签名事件；非流式 block 中是否另有签名或加密载荷。
- 流式 tool call 的 `id`、函数名和参数是否可能分批到达；start/delta/end 必须使用同一个稳定 ID。
- opaque metadata 是否经过 event、agent message、session storage 和 API 转换完整往返。
- 切换 provider、endpoint 或 model 时是否只回传目标模型自己生成的 metadata。
- 多模态输入块是否符合当前 endpoint，而不只是底层模型能力。

常见协议不能互相套用：

| Provider/接入方式 | 典型形态 | 维护注意点 |
| --- | --- | --- |
| OpenAI 官方 | `reasoning_effort` | effort 枚举随模型代际变化 |
| Anthropic 新模型 | `thinking.type=adaptive` + `output_config.effort` | 部分模型 adaptive always-on，旧模型仍使用 `budget_tokens` |
| DashScope 兼容端点 | `extra_body.enable_thinking`，部分模型再带 budget/effort | 托管的 Kimi/GLM 也按 DashScope 协议处理 |
| Kimi 官方 | 新旧代际可能分别使用 `reasoning_effort`、always-on 或 `extra_body.thinking` | 必须逐模型核验 |
| Z.AI 官方 | `extra_body.thinking.type` | 不要因兼容 OpenAI SDK 就改成 OpenAI 字段 |
| Gemini OpenAI-compatible | 以该兼容文档支持的字段为准 | 不要从原生 Gemini API 参数直接推导 |

### Opaque metadata 的端到端约束

本仓库用 `provider_metadata` 承载只能由 adapter 解释的数据。增加这类字段时必须同时检查：

1. `providers/base.py` 的 `ContentBlock` 和非流式响应是否能承载它。
2. `types/stream_events.py` 是否能把流式签名与对应 block/tool call 关联起来。
3. `agent/message.py`、`agent_loop.py` 和 session 序列化是否原样保存，而不是只拼接可见文本。
4. metadata 是否记录实际产生它的 provider 身份，而不是硬编码父类协议名。
5. 对应 adapter 是否只读取当前 provider/endpoint/model 的 metadata，并按官方 wire format 回传；
   派生的 compatible provider 或 fallback 模型不能复用其他模型的签名或加密载荷。
6. `cli/output_formats.py`、A2A、ACP、UI、遥测和日志是否不会公开 opaque payload。

不要把 signature 拼到 reasoning 文本，也不要把 redacted block 转成普通文本。Anthropic 的 thinking
signature/redacted thinking 和 Gemini function-call thought signature 都属于协议状态；丢失或修改后，
后续带工具结果的请求可能直接返回 4xx。

### 降级、缓存和遥测

修改 [`src/iac_code/providers/manager.py`](../src/iac_code/providers/manager.py) 中的
`MODEL_FALLBACK_MAP` 时，降级模型必须：

- 在相同 provider/endpoint 上可用；
- 支持当前请求需要的模态和工具能力；
- 不形成循环；
- 尽量保持同一协议族，并明确质量/成本取舍。

重试分类也要用实际 SDK 异常验证。OpenAI、Anthropic 等 SDK 的连接/超时异常通常不继承 Python
内置 `ConnectionError` 或 `TimeoutError`；只用内置异常写测试会让真实网络错误绕过重试和 fallback。
认证、权限、参数错误等非瞬时 4xx 则必须直接失败，不能遍历降级链。

如果新模型支持显式上下文缓存，检查
[`src/iac_code/providers/dashscope_provider.py`](../src/iac_code/providers/dashscope_provider.py)
中的显式缓存支持列表及消息标记逻辑。

修改 [`src/iac_code/services/telemetry/constants.py`](../src/iac_code/services/telemetry/constants.py)
中的 `KNOWN_MODELS`，否则新模型会被规范化为 `other`。这里不得放 API key、endpoint 中的账号信息
或用户自定义 deployment 名。

### UI、认证和配置

registry 会驱动多个入口，更新后至少检查：

- `/auth` 展示的 provider 默认模型；
- `/model` 和模型选择器的列表、默认项、多模态标记；
- 已有 `settings.yml` 中旧模型是否仍能读取；
- custom provider 和自定义 model ID 是否未被限制；
- Azure 或其他 deployment-based provider 是否错误地把公开 model ID 当作 deployment ID。

仅修改现有 model ID 通常不需要新增用户可见字符串，因此一般不需要运行 `make translate`。
若修改了 provider 显示名、错误提示或帮助文本，则按仓库规则更新翻译。

## 测试策略

### 必测内容

1. registry：新增/移除模型、默认模型、多模态标记、区域/套餐差异。
2. thinking registry：family、effort 枚举、默认值、budget 和 fallback。
3. adapter wire format：开启、关闭、默认、非法 effort，以及代际差异。
4. manager：降级映射和 provider 选择。
5. UI/auth：默认模型和可选模型是否来自 registry。
6. telemetry：新增模型不会被清洗为 `other`。
7. opaque metadata：流式、非流式、会话序列化、下一轮回传和公开输出隔离。

本仓库的主要测试位置：

```text
tests/providers/test_provider_model_research_updates.py
tests/providers/test_thinking_registry.py
tests/providers/test_openai_provider.py
tests/providers/test_anthropic_provider.py
tests/providers/test_dashscope_provider.py
tests/providers/test_deepseek_provider.py
tests/providers/test_manager.py
tests/agent/test_message.py
tests/agent/test_agent_loop_new.py
tests/services/test_token_counter.py
tests/services/test_context_manager.py
tests/commands/test_auth_basics.py
tests/cli/test_output_formats.py
tests/types/test_stream_events.py
tests/test_config_env_overrides.py
tests/ui/test_stream_accumulator.py
tests/ui/test_renderer_events.py
tests/ui/dialogs/test_model_picker.py
tests/test_services/test_telemetry/test_constants.py
```

先运行聚焦测试：

```bash
uv run pytest \
  tests/providers \
  tests/agent/test_message.py \
  tests/agent/test_agent_loop_new.py \
  tests/services/test_token_counter.py \
  tests/services/test_context_manager.py \
  tests/cli/test_output_formats.py \
  tests/types/test_stream_events.py \
  tests/test_config_env_overrides.py \
  tests/ui/test_stream_accumulator.py \
  tests/ui/test_renderer_events.py \
  tests/ui/dialogs/test_model_picker.py \
  tests/commands/test_auth_basics.py \
  tests/test_services/test_telemetry/test_constants.py
```

provider、配置或共享逻辑变化完成后，再运行：

```bash
make test
make lint
```

如需真实 API smoke test，使用开发者自己的已配置凭据，只发送最小请求，并分别验证文本、思考、
工具调用和图片输入。不要打印请求 header、API key 或用户配置，也不要把真实响应录入测试 fixture。

## 提交前检查表

```bash
git diff HEAD --check
git status --short
git diff HEAD -- src/iac_code tests scripts
git diff HEAD -- iac-code-rs
```

确认以下事项后再提交：

- 每个 model ID 都能追溯到官方来源和核验日期。
- 默认模型有明确理由，且没有多个默认项。
- preview、deprecated、alias 和 dated snapshot 没有混为一谈。
- 同名模型在不同 provider 下的思考协议没有被错误复用。
- 新模型已加入遥测白名单和必要的降级链。
- 聚焦测试、全量测试和 lint 已通过，或在 PR 中明确记录未运行项。
- `git diff HEAD -- iac-code-rs` 为空。
- 提交中没有调研缓存、网页快照、API 响应、密钥或用户配置。

## 2026-07 更新案例中的经验

这次更新以当时的 `main` 分支为基线，暴露了几个以后仍然适用的规律：

- OpenAI 新代际增加了新的 effort 档位，不能让所有旧模型共享同一枚举。
- Anthropic 同时存在 adaptive thinking、adaptive always-on 和 legacy budget 三类行为。
- 模型存在于官方目录不等于当前 adapter 可调用；只支持 Responses API 的模型不能放进固定使用
  Chat Completions 的 provider 列表。
- Kimi 不同代际分别使用 effort、always-on 和 `thinking.type`，仅看“Kimi 是 OpenAI-compatible”
  会生成错误请求。
- 百炼标准端点与 Token Plan 的模型集合不同；官方 Kimi/GLM 文档也不能证明百炼上的 model ID
  和参数形态。
- Gemini preview alias 的增删需要同时更新列表和“旧 ID 已移除”的回归测试。
- 工具调用能在第一轮成功并不能证明 adapter 完整；还必须验证下一轮是否原样回传 provider signature。
- registry 更新后如果遗漏遥测白名单、模型选择器或降级链，基础调用可能正常，但产品行为仍不完整。

### 2026-07-21 证据附录

本表用于说明本次代码为何这样实现，并与回归测试互相核对。它是带日期的历史证据，不替代下次更新
时的重新调研。

| 范围 | 2026-07-21 核验结论 | 代码/测试落点 | 官方证据 |
| --- | --- | --- | --- |
| OpenAI | 当前 `OpenAIProvider` 固定调用 Chat Completions，因此排除 `gpt-5.5-pro`、`gpt-5.4-pro`、`gpt-5.2-pro` 等 Responses-only 模型；`gpt-5.3-codex` 支持 Chat Completions 及 `low/medium/high/xhigh`，应保留；`gpt-5.6`、`gpt-5.5`、`gpt-5.4` 的 context 为 105 万、最大输出为 12.8 万，`gpt-5.4-mini/nano`、`gpt-5.3-codex`、`gpt-5.2` 为 40 万/12.8 万；`gpt-5.6` 系列单独支持 `none` 和 `max`，`gpt-5.5` 默认 effort 为 `medium`；SDK 的 connection/timeout、408/409/429 和所有 5xx 异常必须进入重试和 fallback，而 `o3/o4-mini` 不开放 `none` | `registry.py`、`thinking.py`、`openai_provider.py`、`manager.py`、`context_manager.py` 及对应测试 | [Models](https://developers.openai.com/api/docs/models)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5)、[OpenAI Python retries](https://github.com/openai/openai-python#retries)、[GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4)、[GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)、[GPT-5.3 Codex](https://developers.openai.com/api/docs/models/gpt-5.3-codex)、[GPT-5.2](https://developers.openai.com/api/docs/models/gpt-5.2) |
| Azure OpenAI | API 请求使用用户创建的 deployment name；静态 registry 不预填公开模型 ID，认证时走自定义 model/deployment | `registry.py`、provider 调研回归测试 | [Create and deploy](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/create-resource?view=foundry-classic) |
| Anthropic | Fable 5 adaptive thinking always-on，context 为 100 万、最大输出为 12.8 万；Fable 以 HTTP 200 和 `stop_reason=refusal` 返回拒绝，因此流式消息要暂存到 `MessageEnd` 确认可接受后再向下游发布，拒答片段必须直接丢弃并只降级到获准的 Opus 4.8；即使 Opus 出现可重试故障，也不得沿普通降级链继续到 Sonnet；一旦 Opus 接受请求，当前会话的后续轮次必须固定到 Opus，直到会话重置或重新配置，避免再次请求 Fable 并丢弃 Opus metadata；Opus 4.8 与 Sonnet 5 的 context 均为 100 万、最大输出为 12.8 万；Sonnet 5 可关闭思考；4.6 模型支持 `max` 但不支持 `xhigh`，显式 `budget_tokens` 仍可使用，provider/model settings 中的 `thinkingBudget` 必须实际透传；工具循环必须原样回传 thinking signature 和 redacted thinking，且不得跨 provider、endpoint 或 model 回传；可见 thinking 与 opaque metadata 都要计入上下文估算 | `thinking.py`、`anthropic_provider.py`、`manager.py`、`agent/message.py`、`context_manager.py`、`token_counter.py` 及对应测试 | [Models](https://platform.claude.com/docs/en/about-claude/models/overview)、[Sonnet 5](https://platform.claude.com/docs/en/about-claude/models/whats-new-sonnet-5)、[Refusals and fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback)、[Fallback credit](https://platform.claude.com/docs/en/build-with-claude/fallback-credit)、[Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)、[Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)、[Effort](https://platform.claude.com/docs/en/build-with-claude/effort) |
| Gemini | 稳定目录登记 `gemini-3.6-flash`（默认 `medium`）和 `gemini-3.5-flash-lite`（默认 `minimal`），两者均支持 `minimal/low/medium/high`、1,048,576 context、65,536 最大输出和多模态；3.x/2.5 各模型 effort、默认值和可关闭能力不同，2.5 的兼容接口接受 `minimal`；`gemini-2.0-flash` 已于 2026-06-01 下线；Gemini 3 的 function call 和非工具文本 thought signature 都必须原样回传并计入持久化上下文的 token 估算，但只能回传到同一 provider、endpoint 和 model | `registry.py`、`thinking.py`、`gemini_provider.py`、`openai_provider.py`、`context_manager.py`、`telemetry/constants.py`、`token_counter.py` 及 Gemini 回归测试 | [Latest models](https://ai.google.dev/gemini-api/docs/latest-model)、[Thinking](https://ai.google.dev/gemini-api/docs/thinking)、[Models](https://ai.google.dev/gemini-api/docs/models)、[OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)、[Thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)、[Changelog](https://ai.google.dev/gemini-api/docs/changelog) |
| DashScope / Token Plan | Token Plan 的 `qwen3.8-max-preview` 支持视觉输入、1M context，只能思考，effort 为 `low/medium/xhigh`；它不发送 `enable_thinking`，工具循环通过 `extra_body.preserve_thinking=true` 保留思考状态；该参数仅向端点参数文档明确列出的 Qwen 与 Kimi 模型发送，包含 `qwen3.6-flash`；`thinking_budget` 在 OpenAI-compatible Chat 参数页中限定于 Qwen3.x，GLM-5.2 只发送 `enable_thinking`、`reasoning_effort` 和总输出限制；DashScope 的 `glm-5.1` 以及 Token Plan 的 `glm-5.1/glm-5` 接受 `none/minimal/low/medium/high/xhigh`，不得误用仅 `glm-5.2` 支持的 `max`；`MiniMax/MiniMax-M3` 使用 adaptive/disabled thinking；已核实的百炼容量要按 model ID 精确配置，不能统一落到 `qwen` 的 128K 默认值 | `registry.py`、`thinking.py`、`dashscope_provider.py`、`context_manager.py`、DashScope 调研回归测试 | [OpenAI-compatible Chat](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[Token Plan](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)、[文本生成](https://help.aliyun.com/zh/model-studio/text-generation-model)、[GLM on DashScope](https://help.aliyun.com/zh/model-studio/glm)、[深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)、[Kimi on DashScope](https://help.aliyun.com/zh/model-studio/kimi-api-by-moonshot-ai)、[MiniMax on DashScope](https://help.aliyun.com/en/model-studio/minimax-api-by-minimax) |
| DashScope K3 接入边界与容量 | `kimi/kimi-k3` 上下文为 100 万 token，仅支持公网 URL 图片和隐式缓存；当前附件适配器只生成 Data URL，因此不开放多模态标记；K3 的 `preserve_thinking` 默认关闭，工具循环必须显式开启；最大输出缺少精确证据，运行时保持 8192 的保守值 | `registry.py`、`dashscope_provider.py`、`context_manager.py` 及对应测试 | [K3 上架说明](https://help.aliyun.com/zh/model-studio/newly-released-models)、[Kimi on DashScope](https://help.aliyun.com/zh/model-studio/kimi-api-by-moonshot-ai)、[Context Cache](https://help.aliyun.com/zh/model-studio/context-cache) |
| Kimi 官方 | K3 始终思考并支持 `low/high/max`，context 为 100 万；K2.7 始终思考，关闭参数会报错；K2.6 可启停；K2.5/K2.6/K2.7 的 context 为 256K | `kimi_provider.py`、`context_manager.py`、Kimi 调研回归测试 | [Kimi API Platform](https://platform.kimi.ai/)、[Models overview](https://platform.kimi.ai/docs/api/models-overview) |
| MiniMax 官方 | 中国/国际直连 provider 使用 MiniMax 官方兼容端点，默认模型 `MiniMax-M3` 的 context 为 100 万；DashScope 托管的 `MiniMax/MiniMax-M3` 仍按该端点文档配置为 196,608，二者必须按完整 model ID 分别验证 | `registry.py`、`thinking.py`、`minimax_provider.py`、`context_manager.py`、direct provider 调研回归测试 | [Models](https://platform.minimax.io/docs/guides/models-intro)、[API Overview](https://platform.minimax.io/docs/api-reference/api-overview) |
| Z.AI / GLM 官方 | 中国、国际和 Coding Plan provider 均登记 `glm-5.2`，使用 Chat Completions 的 `thinking.type=enabled/disabled` 协议；直连 GLM-5.2 支持 `reasoning_effort=high/max`、默认 `max`，context 为 100 万、最大输出为 12.8 万 | `registry.py`、`thinking.py`、`zhipu_provider.py`、`context_manager.py`、direct provider 调研回归测试 | [GLM-5.2](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2)、[Migration](https://docs.bigmodel.cn/cn/guide/start/migrate-to-glm-new)、[Chat Completion](https://docs.z.ai/api-reference/llm/chat-completion) |
| DeepSeek | V4 Pro/Flash 通过 Chat Completions 提供思考能力，仓库仅开放官方确认的 `high/max` 档位 | `registry.py`、`thinking.py`、DeepSeek provider 测试 | [Models and pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| 遥测与公开输出 | 所有静态 registry model ID 都应进入有限白名单；provider signature 只作为内部协议状态，不进入 `stream-json` | `telemetry/constants.py`、`cli/output_formats.py` 及对应测试 | 仓库内隐私边界和本次官方模型证据 |

这些内容只用于说明调研方法，不能作为下一次更新的事实来源。模型、参数、区域和套餐可用性都
必须在每次更新时重新核验。

### 2026-08-03 增量证据附录

本节记录 2026-08-03 在上述基线之后重新核验的模型变化；若与 2026-07-21 表格冲突，以本节为准。

| 范围 | 2026-08-03 核验结论 | 代码/测试落点 | 官方证据 |
| --- | --- | --- | --- |
| DashScope / Token Plan | 正式模型 `qwen3.8-max` 已同时进入标准百炼与 Token Plan，支持视觉输入、1M context、Function Calling、结构化输出和显式缓存；它是默认开启思考的混合思考模型，接受 `low/medium/xhigh`，可显式关闭，并要求工具循环完整回传 `reasoning_content`。预览模型 `qwen3.8-max-preview` 仍仅在 Token Plan 可用且保持仅思考模式。Token Plan 团队版同时列出 `deepseek-v4-flash` 与 `deepseek-v4-flash-0731`，个人版列出后者，因此共享目录保留两个 ID | `config.py`、`registry.py`、`thinking.py`、`dashscope_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [模型列表](https://help.aliyun.com/zh/model-studio/models)、[文本生成](https://help.aliyun.com/zh/model-studio/text-generation-model)、[深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)、[OpenAI-compatible Chat](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[Context Cache](https://help.aliyun.com/zh/model-studio/context-cache)、[Token Plan 个人版](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)、[Token Plan 团队版](https://help.aliyun.com/zh/model-studio/token-plan-team-overview) |
| DeepSeek 官方 | Chat Completions 的精确 API model ID 仍为 `deepseek-v4-pro`、`deepseek-v4-flash`；`DeepSeek-V4-Flash-0731` 是后者的模型版本而不是新的直连 model ID。两者均支持思考/非思考切换、`low/high/max` effort、1M context 和最大 384K 输出；使用工具时必须在后续请求完整回传 `reasoning_content` | `thinking.py`、`deepseek_provider.py`、`context_manager.py` 及对应测试 | [Models and pricing](https://api-docs.deepseek.com/quick_start/pricing/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) |

### 2026-08-18 增量证据附录

本节记录 2026-08-18 在上述基线之后重新核验的模型变化；若与 2026-07-21 / 2026-08-03 表格冲突，以本节为准。

| 范围 | 2026-08-18 核验结论 | 代码/测试落点 | 官方证据 |
| --- | --- | --- | --- |
| Gemini | `gemini-3.7-flash` 于 2026-08-13 GA（介绍价至 2026-12-31），成为新的默认模型：输入 1,048,576 / 输出 65,536，思考档位为 `low/medium/high`，`minimal` 会直接报错（因此不复用含 MINIMAL 的 `_GEMINI_3_EFFORTS`）；支持文本/图片/视频/音频/PDF 输入、Function Calling、结构化输出与缓存。`gemini-3.1-pro-preview` 与 `gemini-3.1-pro-preview-customtools` 仍在 preview 目录中，予以保留；`gemini-3.6-flash` 降为非默认 | `registry.py`、`thinking.py`、`context_manager.py`、`manager.py`、遥测及对应测试 | [Models](https://ai.google.dev/gemini-api/docs/models)、[Release notes](https://ai.google.dev/gemini-api/docs/changelog)、[Gemini 3.7 Flash 模型页](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)、[Gemini 3.1 Pro 模型页](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview) |
| DeepSeek 官方 | 2026-08-13 发布 DeepSeek V4 Pro 正式版（内部版本 DeepSeek-V4-Pro-0813），公告明确「API 模型名不变」，直连 model ID 仍为 `deepseek-v4-pro`/`deepseek-v4-flash`，因此直连 provider 目录不变。思考模式现支持 `low/high/max` 三档（默认 `high`；`medium`/`xhigh` 服务端映射为 `high`），仓库的 `_DEEPSEEK_EFFORTS` 已覆盖。DeepSeek API 另原生支持 Responses API 并适配 Codex，本仓库仍走 Chat Completions，不改 adapter。峰谷定价自 2026-08-17 生效，不影响参数 | 无代码变更（能力已在 2026-08-03 基线覆盖） | [DeepSeek-V4-Pro 正式版上线](https://api-docs.deepseek.com/zh-cn/news/news260813)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) |
| DashScope / Token Plan | `deepseek-v4-pro-0813` 于 2026-08-13 上架百炼（华北2/新加坡），为混合思考模型（`enable_thinking` 切换），同时进入 Token Plan 个人版与团队版目录；按 `deepseek-v4-pro` 同规格建模（1M context、384K 输出），仅存在于百炼端点，故在 `config.py` 精确映射到 `dashscope`（含 `deepseek-v4-flash-0731` 一并补齐）。`qwen3.8-max-preview` 已结束预览并正式下线，旧 ID 仍可调用且自动路由至 `qwen3.8-max`，因此移出可选目录但保留 fallback/context/遥测映射以兼容既有 `settings.yml`。`qwen3.7-flash`（2026-07-21 上架，视觉理解、1M context、思考、Function Calling、内置工具、结构化输出）补入目录。`qwen3.8-2.4t-a95b`（2026-08-12 上架的开源版旗舰，1M context）仅记录不收录——目录惯例不含开源权重 Qwen。Token Plan 个人版目录不再列出 `qwen3.6-plus`（团队版仍列出），共享目录保留 | `config.py`、`registry.py`、`thinking.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [模型上下架与更新](https://help.aliyun.com/zh/model-studio/newly-released-models)、[DeepSeek-阿里云](https://help.aliyun.com/zh/model-studio/deepseek-api)、[文本生成](https://help.aliyun.com/zh/model-studio/text-generation-model)、[Token Plan 个人版](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)、[Token Plan 团队版](https://help.aliyun.com/zh/model-studio/token-plan-team-overview) |
| Z.AI / GLM 官方 | GLM-5.3 于 2026-08-14 发布：仅文本输入，1M context、最大输出 128K；思考始终开启，`thinking.type` 仅接受 `enabled`，`reasoning_effort` 支持 `low/high/max`（默认 `max`），发送 `disabled` 会失败——官方迁移建议为改用 `enabled` + `reasoning_effort=low`，adapter 对关闭思考的请求按此降级。GLM-5.3 当前已在 GLM Coding Plan 全量上线（CN/Intl coding 端点），标准模型 API「近期上线」，因此只登记到 `zhipu_cn_codingplan`/`zhipu_intl_codingplan` 并成为默认，直连 `zhipu_cn`/`zhipu_intl` 不登记（思考注册表保持 NONE） | `config.py`、`registry.py`、`thinking.py`、`zhipu_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [GLM-5.3（CN）](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)、[GLM-5.3（Intl）](https://docs.z.ai/guides/llm/glm-5.3) |
| Kimi 官方 | 无新模型；K3 仍为旗舰。公告：`kimi-k2.5` 与 `moonshot-v1` 系列不再对新注册用户开放，全平台 2026-08-31 下线；存量用户在此之前仍可调用，目录暂不删除。DashScope/火山托管的同名模型不受该公告影响 | 无代码变更（目录保留 `kimi-k2.5`） | [Model List](https://platform.kimi.ai/docs/models) |
| MiniMax / OpenAI / Anthropic | MiniMax 无新文本模型（M3 仍为旗舰；H3 为视频模型，不在范围内）。OpenAI 无新增通用模型：GPT-5.6 Sol「Ultrafast」是服务层级而非新 model ID（2026-08-13，受限预览），`gpt-5.6-cyber` 仅限 Daybreak Red 审批客户，均不进入目录。Anthropic 无新 GA 模型：`claude-mythos-5` 属 Project Glasswing 邀请制，不进入目录 | 无代码变更 | [OpenAI Changelog](https://developers.openai.com/api/docs/changelog)、[Anthropic Models](https://platform.claude.com/docs/en/about-claude/models/overview)、[MiniMax Models](https://platform.minimax.io/docs/guides/models-intro) |
| 降级链实现 | `_get_fallback_model` 放宽为「仅要求降级目标仍在 provider 目录内」：源模型可能是已移出可选目录但仍可调用的遗留 ID（如 `qwen3.8-max-preview`），其保存的配置仍应享受降级保护；空目录 provider（compatible/azure/ollama 等）不受影响 | `manager.py` 及 `tests/providers/test_manager.py` | 本次官方模型证据与仓库内降级规则 |

### 2026-08-27 增量证据附录

本节记录 2026-08-27 在上述基线之后重新核验的模型变化；若与更早的证据附录冲突，以本节为准。

| 范围 | 2026-08-27 核验结论 | 代码/测试落点 | 官方证据 |
| --- | --- | --- | --- |
| DashScope / Qwen | 按完整百炼 Chat Completions 模型目录重新核验，而不是只看 2026-08-18 之后的增量：新增 `qwen3.8-flash`、优速模式 `qwen3.8-max-prime`、开源服务 `qwen3.8-2.4t-a95b` / `qwen3.8-27b`，并补齐 `qwen3.6-35b-a3b` / `qwen3.6-27b`。官方没有 `qwen3.8-35b`：Qwen3.8 当前开放权重只有 2.4T-A95B 与 27B，35B 的精确 ID 属于 Qwen3.6。Qwen3.8 两款开源服务均为 1M context / 131,072 最大输出并支持显式缓存；Qwen3.6 两款为 262,144 / 65,536 且不支持显式缓存。上述开源模型为可启停的混合思考模型，使用 `enable_thinking` 与可选 `thinking_budget`，不套用仅 Max 支持的 `reasoning_effort`。`qwen3.8-flash` 同时进入标准百炼与 Token Plan 个人版，并支持视觉输入和 1M context；其最大输出未在当前公开页给出，运行时继续使用保守默认值 | `registry.py`、`thinking.py`、`dashscope_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [模型上下架与更新](https://help.aliyun.com/zh/model-studio/newly-released-models)、[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[Qwen3.8-2.4T-A95B](https://help.aliyun.com/zh/model-studio/qwen3-8-2-4t-a95b)、[Qwen3.8-27B](https://help.aliyun.com/zh/model-studio/qwen3-8-27b)、[Qwen3.6-35B-A3B](https://help.aliyun.com/zh/model-studio/qwen3-6-35b-a3b)、[Qwen3.6-27B](https://help.aliyun.com/zh/model-studio/qwen3-6-27b)、[深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)、[优速模式](https://help.aliyun.com/zh/model-studio/fast-mode)、[Token Plan 个人版](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview) |
| DashScope / 第三方直供 | 补入百炼自部署 `kimi-k3`（与月之暗面直供的 `kimi/kimi-k3` 是不同服务边界）、小米直供 `xiaomi/mimo-v2.5-pro` 和阶跃星辰直供 `stepfun/step-3.7-flash`。百炼 `kimi-k3` 为仅思考模型，`enable_thinking` 不可关闭且 `preserve_thinking` 默认开启，支持 Base64 图片；MiMo 为默认开启但可关闭的混合思考纯文本模型，不支持 effort/budget/preserve；Stepfun 为多模态混合思考模型，支持 `low/medium/high` effort，不支持 budget/preserve。三个命名空间/重名模型均按百炼兼容端点协议单独登记，避免复用厂商直连协议 | `config.py`、`registry.py`、`thinking.py`、`dashscope_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [百炼 Kimi](https://help.aliyun.com/zh/model-studio/kimi-api)、[kimi-k3 模型信息](https://help.aliyun.com/zh/model-studio/aliyun-kimi-k3)、[MiMo](https://help.aliyun.com/zh/model-studio/mimo)、[MiMo-V2.5-Pro 模型信息](https://help.aliyun.com/zh/model-studio/mimo-v2-5-pro)、[Stepfun](https://help.aliyun.com/zh/model-studio/stepfun)、[Step 3.7 Flash 模型信息](https://help.aliyun.com/zh/model-studio/step-3-7-flash) |
| Z.AI / GLM 官方 | `glm-5.3` 已同时开放标准 Model API 与 Coding Plan，不再只限 Coding Plan，因此进入中国站、国际站的标准 provider 并成为默认；2026-08-26 新发布 `glm-5.3-flash`，同时开放 Model API 与 Coding Plan，支持 1M context、原生图片/视频/文件输入、Function Calling 与结构化输出。其文本参数与 GLM-5.3 一致：思考始终开启，`thinking.type` 仅接受 `enabled`，`reasoning_effort` 支持 `low/high/max`（推荐 `max`）；当前附件适配器使用官方明确支持的 Base64 Data URL，因此开放图片输入标记。关闭思考时继续按 GLM-5.3 迁移规则降级为 `enabled + low` | `config.py`、`registry.py`、`thinking.py`、`zhipu_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [GLM-5.3（CN）](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)、[GLM-5.3（Intl）](https://docs.z.ai/guides/llm/glm-5.3)、[GLM-5.3-Flash](https://docs.z.ai/guides/vlm/glm-5.3-flash)、[发布公告](https://z.ai/blog/glm-5.3-flash) |
| DashScope 智谱直供 | 百炼华北2（北京）新增智谱原厂直供 `ZHIPU/GLM-5.3`，精确 model ID 包含大写命名空间；仅文本输入，context 为 1,048,576、最大输出为 131,072，支持 Function Calling 与隐式缓存。模型信息页把结构化输出标为支持，但端点级调用页标为不支持，本次不依赖该能力并以端点级限制为准。模型始终思考，`enable_thinking` 必须保持 `true`，`reasoning_effort` 接受 `low/high/max`；关闭请求降级为 `enable_thinking=true + low`。该模型未进入 Token Plan 目录，且按百炼兼容端点协议发送参数，不能复用 Z.AI 直连的 `thinking.type` wire format | `config.py`、`registry.py`、`thinking.py`、`dashscope_provider.py`、`manager.py`、`context_manager.py`、遥测及对应测试 | [GLM-智谱直供调用](https://help.aliyun.com/zh/model-studio/glm-zhipu)、[ZHIPU/GLM-5.3 模型信息](https://help.aliyun.com/zh/model-studio/glm-5-3-by-zhipu)、[Token Plan 个人版目录](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview) |
| 其他厂商直连 provider | 重新检查 OpenAI、Anthropic、Gemini、DeepSeek、Kimi 与 MiniMax 的官方模型目录和更新日志后，没有发现 2026-08-18 基线之后适合当前 Chat Completions/Message 直连接入路径的新 GA 文本模型；上表的 `kimi-k3`、MiMo 和 Stepfun 变化只属于百炼服务。Kimi K2.5 的 2026-08-31 下线日期尚未到达，继续保留以兼容存量用户 | 无代码变更 | [OpenAI Models](https://developers.openai.com/api/docs/models)、[Anthropic Models](https://platform.claude.com/docs/en/about-claude/models/overview)、[Gemini Release Notes](https://ai.google.dev/gemini-api/docs/changelog)、[DeepSeek Models](https://api-docs.deepseek.com/quick_start/pricing)、[Kimi Models](https://platform.kimi.ai/docs/models)、[MiniMax Models](https://platform.minimax.io/docs/guides/models-intro) |
