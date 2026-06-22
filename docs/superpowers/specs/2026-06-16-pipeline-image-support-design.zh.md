# Pipeline 图片支持设计

## 目标

Pipeline 模式应支持与普通聊天模式相同的图片输入能力。图片需要贯穿 REPL 输入、A2A 输入、运行中 pipeline 打断、用户问题恢复、候选方案选择恢复，以及进程/会话恢复。

本设计有意把多模态范围限定为图片输入，因为普通模式目前支持的是 image block，而不是任意音频或二进制载荷。

## 已确认决策

- Pipeline 接收用户输入的所有入口都支持图片。
- 允许只有图片、没有文字的输入。
- 尽可能复用普通模式的图片行为。
- 保留早期模型能力检查。如果当前 provider/model 不支持图片，应在开始处理前失败或提示。
- 图片 bytes 以内联 base64 存入 `ImageBlock.data`，与普通模式消息存储一致。
- A2A 图片使用与 REPL 图片相同的处理路径进行 resize/downsample。
- Pipeline 完成后，不把 pipeline 图片带入 normal mode handoff。Handoff 仍只使用文本总结。
- Interrupt judge 可以看到图片，并可以把从图片中识别出的信息摘要写入 `reason` 或 `rollback_context`。
- 最终目标 step 或 candidate 仍必须收到原始 image blocks，而不能只收到 judge 的摘要。

## 架构

新增一个很薄的内部输入包装类型，命名为 `PipelineUserInput`，作为所有 pipeline 用户输入边界的统一形态。

```python
@dataclass(frozen=True)
class PipelineUserInput:
    content: str | list[ContentBlock]
    display_text: str
    has_images: bool
```

`content` 是传给 `AgentLoop` 的事实来源。它可以是纯字符串，也可以是由 `TextBlock` 和 `ImageBlock` 组成的结构化列表。`display_text` 用于 UI 渲染、A2A 状态事件、日志、sidecar 文本字段和 interrupt prompt 文本。`has_images` 用来让调用方把纯图片输入视为非空输入。

这个包装类型不替代 `Message`，也不修改 provider API。它只用于避免 REPL、A2A、pipeline runner、sidecar 和 interrupt 代码各自发明不同的 `str | list[ContentBlock]` 处理方式。

## REPL 输入流

Pipeline 模式不应再丢弃 `PromptInputResult.pasted_contents`。

当 REPL 收到 pipeline 输入时：

1. 如果输入已经是纯字符串，创建 `PipelineUserInput(content=text, display_text=text, has_images=False)`。
2. 如果输入是 `PromptInputResult`，调用现有的 `process_user_input(text, pasted_contents=...)`。
3. 如果结果中有任何 `ImageBlock`，保留结构化 block list 作为 `content`。
4. 如果没有 image block，保留纯文本作为 `content`。
5. 根据用户可见的 prompt 文本计算 `display_text`。对于纯图片输入，使用安全占位符，例如 `[Image input]`。

当前提示图片会被忽略的 pipeline warning 应删除，或改成测试来证明图片会被转发。

现有图片 attach 路径已经通过 `is_model_multimodal(...)` 做能力门控。Pipeline 应复用该行为，并且不接受普通模式会拒绝的粘贴图片。

## A2A 输入流

A2A 目前会把类似图片的 parts 转成文本 manifest。Pipeline 图片支持需要新增一条转换路径，返回内部 content blocks。

转换器应保留现有文本处理：

- Text parts 转成 `TextBlock`。
- `application/json` 的 JSON data parts 继续序列化成紧凑文本。
- Raw text 和文本 file URL 继续转成文本。

对于支持的图片媒体类型：

- `raw` image bytes 直接从 part 中读取。
- `data` image parts 从 `bytes` 或 `base64` 等字段读取 base64 bytes。
- `file://` image URL parts 从安全的 workspace-local 路径读取 bytes，并保留现有 workspace 和 symlink escape 检查。
- 图片 bytes 经过共享的 resize/downsample helper。
- Resize 后的 bytes 进行 base64 编码，并输出为 `ImageBlock(media_type=..., data=...)`。

Pipeline 模式应把只有图片的 A2A 请求视为有效输入，不应再用 text-only message 让它失败。如果请求包含图片输入，而所选模型不是多模态模型，A2A 应返回清晰的失败状态，而不是静默降级成文本。

音频和 `application/octet-stream` 不包含在本功能范围内。它们可以保持现有 manifest 行为，或在新的 pipeline 多模态转换器中被拒绝，但不能变成 image blocks。

## Pipeline Runner 流程

`PipelineRunner.run`、`resume`、`continue_from_sidecar`、`handle_user_interrupt` 以及相关 A2A bridge 调用应接受 `str` 或 `PipelineUserInput`。内部统一 normalize 成 `PipelineUserInput`。

第一个 step、恢复的 step 或被注入的目标 AgentLoop 接收 `PipelineUserInput.content`。

Pipeline 状态事件和 observability 继续使用由 `display_text` 派生的文本安全字段。输入长度指标应使用 `len(display_text)`，如果有帮助，也可以增加 `has_images` 之类的布尔字段。Telemetry content capture 仍只记录文本，绝不记录 base64 图片数据。

`StepExecutor` 已经接受 `str | list[ContentBlock]`，所以大多数 step 执行逻辑可以保持不变。需要文本的辅助函数，例如 completion guards 或 prompt context snapshots，应使用从 blocks 中提取出的文本，或使用 `display_text`。

## 持久化与恢复

Pipeline step transcript 是恢复 LLM 上下文的事实来源。它们已经用 JSONL 存储 `Message.to_dict()`，并通过 `Message.from_dict()` 读取，因此可以 round-trip `ImageBlock` 内容。

持久化规则如下：

- Pipeline transcripts 存储完整的结构化 `Message(content=list[ContentBlock])`，包括图片 base64。
- Root visible session history 只为 pipeline-visible 用户 turn 存储 `display_text`。
- Sidecar state machine 的 `current_step_user_input` 保持 text-only `display_text`。它是可读的恢复提示，不是多模态内容来源。
- 会话恢复应加载修复后的 pipeline transcripts，并保留 image blocks。
- Cache cleanup 不应影响恢复，因为 transcript 内联包含 base64 图片数据。

这样可以让 sidecar metadata 保持较小，同时在真正需要的地方保留完整图片上下文。

## Interrupt Judge

`InterruptController.judge` 应接受 normalize 后的 pipeline 输入，并把路由 prompt 文本和所有 image blocks 都发送给 judge 模型。

对于文字加图片输入，judge 请求应包含：

- 一个 `TextBlock`，包含当前 pipeline 状态、路由指令和用户 display text。
- 用户输入中的原始 `ImageBlock` 值。

对于纯图片输入，text block 应明确说明用户提供了图片输入，并要求 judge 结合图片判断路由。

`InterruptVerdict` 仍是文本导向的。Judge 可以把从图片中识别出的细节写入 `reason` 或 `rollback_context`，例如：“上传的架构图显示 ECS 通过 SLB 连接 RDS；应回滚到 architecture planning。”

Supplement 行为：

- 目标 parent step 或 candidate AgentLoop 接收原始 `PipelineUserInput.content`。
- Judge 从图片中得到的文本只用于路由，不作为额外替代消息注入。

Hard interrupt 行为：

- 回滚目标同时接收 judge 的 `rollback_context` 和原始图片输入。
- 可以通过在原始 content blocks 前面 prepend 一个包含 `rollback_context` 的 `TextBlock` 来表示。
- 原始 image blocks 必须保留，让目标 step 能独立检查图片。

如果 judge 失败或超时，继续使用现有 interrupt failure policy。实现不能静默丢弃图片输入。

## 错误处理

REPL：

- 如果当前模型不支持图片粘贴，复用普通模式 warning，并且不 attach 图片。
- 如果 resize/downsample 失败，复用普通模式图片错误处理。
- 如果图片成功 attach，纯图片输入也是有效输入。

A2A：

- 无效 base64、不安全 file URL、非文件路径、symlink escape、超大图片和不支持的图片媒体类型，都返回清洗过的失败状态。
- 错误消息不能泄漏本地文件路径或 base64 内容。
- 图片输入遇到非多模态模型时，返回清晰失败状态。

Pipeline：

- 当目标路径期望真实图片支持时，任何 pipeline 分支都不应把图片输入转换成 text-only manifest。
- 空文本加图片是有效输入。没有文字也没有图片时，在今天无效的地方仍然无效。

## 测试计划

REPL 测试：

- Pipeline 模式不再提示图片会被忽略。
- 带图片的 `PromptInputResult` 会用结构化 content 调用 pipeline handler。
- 纯图片输入可以被接受。
- 非多模态模型仍在 attach 前失败，和普通模式一致。

A2A part 转换测试：

- Raw image part 转成 `ImageBlock`。
- Base64 data image part 转成 `ImageBlock`。
- 安全的 file URL image part 转成 `ImageBlock`。
- A2A 图片会调用 resize/downsample。
- 不安全 file URL、无效 base64、超大内容和不支持的图片媒体类型会安全失败。

A2A executor 测试：

- Pipeline 模式把结构化 `PipelineUserInput` 传给 pipeline executor。
- 只有图片的请求是有效的。
- 图片输入遇到非多模态模型时返回清晰失败。

Pipeline runner 测试：

- `run`、`resume`、`continue_from_sidecar` 和 `handle_user_interrupt` 都接受 `PipelineUserInput`。
- Sidecar 保存 display text，transcripts 保存完整 image blocks。
- 恢复后的 transcripts 保留 image blocks。
- 纯图片输入不会被当成空输入。

Interrupt 测试：

- Judge provider request 包含 image blocks。
- Judge 可以产生从图片中识别出的 `rollback_context`。
- Supplement 会把原始图片输入注入目标 AgentLoop。
- Hard interrupt 会用 rollback context 文本和原始 image blocks 重启目标。

回归测试：

- 现有 text-only pipeline 行为保持不变。
- 现有 normal-mode 图片测试继续通过。
- 现有 A2A text、JSON 和 text-file part 处理保持不变。

## 验证

相关 focused 测试命令：

```bash
uv run pytest tests/ui/test_repl_pipeline_image_warning.py tests/utils/image/test_processor.py tests/providers/test_openai_image_blocks.py
uv run pytest tests/a2a/test_parts.py tests/a2a/test_executor.py tests/a2a/test_pipeline_executor.py
uv run pytest tests/pipeline/engine/test_pipeline_runner.py tests/pipeline/engine/test_pipeline_runner_interrupt.py tests/pipeline/engine/test_pipeline_runner_sidecar_path.py
uv run pytest tests/pipeline/engine/test_interrupt.py tests/pipeline/engine/test_transcript_storage.py
```

实现后，如果可行，运行 `make test`。当前 baseline 已知会因为 `src/iac_code/i18n/messages.pot` 在此 worktree 中缺失而导致 6 个 i18n 测试失败；该 baseline 问题与本图片支持设计无关。
