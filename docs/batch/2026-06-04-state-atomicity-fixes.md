# State Persistence Atomicity Fixes

## 背景

本次变更对应 GitHub issue #80，修复配置、输入历史、会话索引和更新检查中的四类持久化/解析问题。实现先在隔离 worktree `codex/state-atomicity` 中完成并压缩为一个提交，然后 cherry-pick 到当前主 checkout 的 `fix_issue_260603` 分支。

## 变更内容

- 在 `src/iac_code/utils/file_security.py` 新增 `atomic_write_text`，使用同目录临时文件写入、`flush`、`fsync` 和 `safe_replace` 原子替换目标文件，并在失败时清理临时文件。
- 将 `src/iac_code/config.py` 的 `_save_yaml` 从直接 `write_text` 改为先序列化 YAML，再通过 `atomic_write_text` 写入，保留原有私有目录和文件权限限制。
- 将 `src/iac_code/ui/core/input_history.py` 的 `_save` 从截断重写改为先生成完整 JSONL 内容，再通过 `atomic_write_text` 原子替换，保留 session-only 输入不落盘的行为。
- 将 `src/iac_code/services/session_index.py` 的 `_decode_json_string` fallback 从链式 `replace` 改为单次扫描解码，避免截断 JSON 中的字面量 `\\n` 被错误转换为换行。
- 将 `src/iac_code/services/update_checker.py` 的 `_is_newer_version` 在遇到 `InvalidVersion` 时改为返回 `False`，避免非法版本字符串进行字典序比较。

## 兼容性判断

- 配置文件和历史文件格式不变：YAML 仍使用原有 `yaml.dump(..., default_flow_style=False, allow_unicode=True)`，输入历史仍是一行一个带 `iac-code-input-history-v1` 标记的 JSON 对象。
- 文件权限行为不变：`_save_yaml` 和 `InputHistory._save` 仍在写入后调用原有权限限制逻辑。
- 原子写使用目标文件同目录临时文件，避免跨文件系统 rename；对读取者而言不会再暴露空文件或半写文件。
- 更新检查对合法 PEP 440 版本仍使用 `packaging.version.Version` 比较；只有非法版本字符串不再尝试推断大小关系。
- 本次 cherry-pick 没有发生冲突，也没有 `message.po` 冲突。此次代码未新增用户可见翻译字符串，因此未运行 `make translate`。

## 验证

在隔离 worktree 中已执行：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/utils/test_file_security.py tests/services/test_session_index.py tests/test_config.py tests/ui/core/test_input_history.py tests/services/test_update_checker.py -q
PATH="$HOME/.local/bin:$PATH" make lint
PATH="$HOME/.local/bin:$PATH" make test
```

结果：

- 相关回归测试：`137 passed`
- `make lint`：ruff 和 ty 均通过
- `make test`：仍有 4 个既有 i18n baseline 失败，原因是 `src/iac_code/i18n/messages.pot` 缺失；其余 `4028 passed, 1 skipped`

cherry-pick 到 `fix_issue_260603` 后，变更未与该分支已有源码修改重叠，兼容性风险较低。
