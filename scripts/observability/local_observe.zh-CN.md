# Local Observe Tool

`local_observe` 是一个小型本地 OTLP receiver 和 Web UI，用于测试 `iac-code` telemetry。

## 启动

```bash
uv run python scripts/observability/local_observe.py --port 4318 --no-open
```

打开 `http://127.0.0.1:4318`。

## 把 Telemetry 发送到本地工具

在运行 `iac-code` 的 shell 中设置：

```bash
export IAC_CODE_TELEMETRY_ENDPOINT=http://127.0.0.1:4318
export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1
unset DISABLE_TELEMETRY
```

如果需要包含原始 prompt、模型输出和工具 payload：

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT
```

然后正常运行 `iac-code`。

## UI

- 使用 **Demo debug off/on** 加载示例数据。
- 使用 **Expected raw content** 检查 debug-off / debug-on 行为。
- Pipeline records 会按 run、step、AgentLoop round 和 raw evidence 分组。
- Pipeline 级证据会拆分为 lifecycle、pipeline 后的 normal chat，以及其他 session evidence。
- 开始新的手工测试前，使用 **Clear** 清空数据。
- 使用 **Export JSONL** 保存已捕获的记录。
