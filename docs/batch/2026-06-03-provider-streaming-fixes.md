# Provider Streaming Fixes 合入说明

日期：2026-06-03

目标分支：`fix_issue_260603`

来源分支：`provider-streaming`

来源提交：`c56de18 fix: stabilize provider streaming fallback`

目标提交：`3f597d4 fix: stabilize provider streaming fallback`

## 背景

本次合入用于修复 GitHub issue #67 中 provider streaming 相关的稳定性问题。问题集中在流式请求失败后的降级恢复、超时检测，以及 QwenPaw 配置异常处理。

## 本次做了什么

### ProviderManager 流式请求修复

- 将流式事件消费从裸 `async for` 改为显式 async iterator，并用 `asyncio.wait_for()` 限制每次等待下一个事件的时间。
- 当 provider stream 在首个事件前或中途长时间不再产出事件时，会进入现有的 tombstone 加非流式 fallback 恢复路径，而不是无限挂起。
- `asyncio.CancelledError` 会继续向外传播，不会被误当成普通 streaming 失败并触发 fallback。

### 模型 fallback 修复

- 非流式 fallback 使用临时 provider/model 覆盖值，不再修改 `ProviderManager._model` 或缓存的 `ProviderManager._provider`。
- fallback 成功后，会话当前模型仍保持用户选择的 primary model。
- fallback provider 创建失败或 fallback `complete()` 失败时，继续向调用方暴露 primary failure，避免用 fallback 内部错误覆盖主错误。
- 流式失败后使用非流式 fallback 恢复时，success telemetry 和 span response model 会记录实际生成响应的 fallback model/provider。

### QwenPaw 错误处理修复

- `ProviderManager._check_qwenpaw_config_change()` 不再在 provider 层调用 `sys.exit(1)`。
- streaming 路径中的 QwenPaw 配置错误会被转换为非 retryable `ErrorEvent`，让交互进程保持存活。
- 启动阶段已有的 QwenPaw 行为不在本次范围内，保持兼容。

### 测试覆盖

新增和增强了 provider manager 测试，覆盖：

- fallback 成功不永久修改 manager state。
- stream idle timeout 能通过非流式 fallback 恢复。
- stream partial message 后 timeout 会先 tombstone 再 fallback。
- stream cancellation 会传播且不会进入 fallback。
- QwenPaw stream-time 配置错误会产生 `ErrorEvent` 而不是 `SystemExit`。
- fallback provider 创建失败和 fallback `complete()` 失败都会保留 primary error。
- fallback 恢复路径的 telemetry 会记录 fallback model/provider。

## Cherry-pick 结果

在 `/Users/ehzyo/open_repo/iac-code` 中执行 cherry-pick：

```bash
git cherry-pick c56de181446e7db1327907c990723515d9e0e8c4
```

结果：

- cherry-pick 干净完成。
- 没有文件冲突。
- 没有 `message.po` 冲突。
- 未修改翻译文件。

## 翻译处理

本次生产代码没有新增 `_()` 包裹的用户可翻译字符串，主要新增内容是 provider 内部异常类型、stream event 拼装逻辑、测试和文档。

cherry-pick 没有出现 `message.po` 冲突；但目标工作区全量测试时发现 `.mo` 文件时间戳早于 `.po`，`tests/test_i18n.py::test_mo_files_up_to_date` 失败。因此已运行：

```bash
make translate
```

结果：

- `zh/es/fr/de/ja/pt` 的 update 和 compile 均成功。
- `messages.po` 仅更新了 `POT-Creation-Date` 和 `src/iac_code/providers/manager.py` 行号引用。
- 没有新增或删除翻译词条。
- `.mo` 编译后通过 i18n 测试；`.mo` 内容没有产生 git diff。

如果后续改动引入新的 `_()` 字符串，或再次合并时出现 `message.po` 冲突，应重新运行 `make translate`，并检查 `src/iac_code/i18n/locales/*/LC_MESSAGES/messages.po` 中相关词条是否保留完整。

## 兼容性判断

### 公共 API

`ProviderManager.complete()`、`ProviderManager.stream()`、provider base interface 和 CLI 入口均未改变签名。现有调用方不需要改代码。

### Provider 兼容性

当前 LLM provider 的 `stream()` 接口本来就是 async generator。显式调用 `__aiter__()` 和逐次 `__anext__()` 等价于原来的 `async for`，只是增加了每次等待下一个事件的超时边界。

### 行为变化

- streaming hang 会被恢复为 fallback 或 error，而不是无限等待。这是预期修复。
- QwenPaw streaming 配置错误不再退出进程，而是返回 stream error event。这是更保守的交互式行为。
- fallback 成功不再永久降级当前模型。这修复了原先的隐藏状态突变。

### 依赖和数据结构

没有新增依赖，没有数据库或配置 schema 迁移，没有更改用户凭证文件格式。

### 风险

主要风险是某些 provider 在 idle timeout 内没有产出事件时会更早进入 fallback。默认 timeout 仍为 90 秒，保留了较宽松的等待窗口；手动或测试场景可通过 `stream_idle_timeout` 调小。

## 验证建议

推荐在合入后运行：

```bash
PATH="$HOME/.local/bin:$PATH" make lint
PATH="$HOME/.local/bin:$PATH" make test
```

本次在目标工作区实际验证结果：

- `PATH="$HOME/.local/bin:$PATH" make lint`：通过。
- `PATH="$HOME/.local/bin:$PATH" uv run pytest tests/test_i18n.py -v`：`18 passed`。
- `PATH="$HOME/.local/bin:$PATH" make test`：`4017 passed, 244 warnings`。

交互式手测建议覆盖：

- 普通流式请求可以正常返回。
- 长请求中 `Ctrl+C` 能取消，不会触发 fallback 补全。
- 如果使用 QwenPaw，临时设置未知 provider 后发起请求，交互进程不退出，并返回配置错误信息。
