# Scripts

本目录包含本地开发、手工测试和端到端辅助脚本。除非某个脚本自己的 README 另有说明，
否则请从仓库根目录使用 `uv run python ...` 运行脚本。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `a2a/debugger.py` | A2A pipeline stream 的 Web debugger / client。 |
| `a2a/debugger.md` | A2A debugger 的手工使用说明。 |
| `a2a/selling_console.py` | Selling pipeline console 的本地 HTTP server；把纯文本 UI 请求代理到 A2A server。 |
| `a2a/selling_console_web/` | 静态 Selling Console 前端。它负责渲染 pipeline 进度、候选方案卡片、聊天和调试面板；图片输入覆盖范围属于 `a2a/debugger.py`。 |
| `a2a/e2e/` | A2A 会话恢复端到端场景 runner、共享 helper 和结果说明。 |
| `a2a/smoke/test_a2a_vpc.py` | A2A VPC / pipeline 行为的小型手工 smoke 脚本。 |
| `acp/smoke/test_acp_vpc.py` | ACP VPC 行为的小型手工 smoke 脚本。 |
| `headless/smoke/test_headless_vpc.py` | Headless VPC 行为的小型手工 smoke 脚本。 |
| `observability/local_observe.py` | 本地 OTLP observability server 入口。 |
| `observability/local_observe/` | 本地 observe server 实现和静态 Web UI。 |
| `observability/local_observe.md` | 本地 observe 工具的手工使用说明。 |
| `rendering/test_diagram_render.py` | 手工图表渲染检查。 |
| `repl/e2e/` | 基于真实 PTY 驱动的 REPL pipeline 端到端场景 runner。因为使用 `pexpect`，仅支持 POSIX 环境。 |

## 常用命令

```bash
uv run python scripts/a2a/debugger.py --help
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/selling_console.py --port 41980 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
uv run python scripts/a2a/e2e/run_recovery_scenarios.py --help
uv run python scripts/observability/local_observe.py --help
uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

`scripts/repl/e2e/run_pipeline_scenarios.py` 默认把产物写到系统临时目录，主要用于手工验证
或 smoke 验证。它不是 `make test` 的一部分；单元测试只覆盖 helper 行为。真实 PTY runner
依赖仅支持 POSIX 的开发依赖 `pexpect`。

根目录的 `conftest.py` 包含 tiktoken 隔离 fixture，确保测试不会读写开发者真实的 encoding
cache。新增测试应继续走这条 fixture 路径，不要直接使用用户缓存。

Cleanup ledger 的临时文件名使用前导点只是外观约定。正确性依赖 atomic replace、重试和 ledger
校验，而不是 Unix hidden-file 行为。

这些 helper 的 pytest 测试位于 `tests/` 下；本目录里的可执行脚本保留给本地调试、手工验证
和真实端到端运行使用。
