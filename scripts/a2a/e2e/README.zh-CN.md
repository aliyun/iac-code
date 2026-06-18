# A2A 会话恢复 E2E

本目录包含用于 A2A pipeline 会话恢复的 headless 端到端检查。Runner 会驱动公开的
A2A JSON-RPC streaming endpoint，记录 SSE 事件和 pipeline snapshot，用 `SIGKILL`
杀掉 A2A server，再用相同持久化目录重启，并验证会话能按预期的 `contextId` /
`taskId` 继续。

脚本流程刻意贴近 `scripts/a2a/debugger.py` 的手工 Web debugger：`contextId` 表示
一次会话，`taskId` 表示这个会话里的一个 A2A task。

## 快速开始

从仓库根目录运行：

```bash
cd /path/to/iac-code
```

如果只想做最短恢复检查，先跑 deterministic crash smoke。它会在 A2A pipeline
snapshot 保存后注入一次 crash，然后重启 server，并验证 task 恢复 artifact：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --deterministic \
  --skip-preflight \
  --scenario fault-after-snapshot \
  --fault-at after_a2a_pipeline_snapshot_saved
```

如果想快速验证一条真实 provider / tool / 云路径，跑一个代表场景：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model qwen3.6-plus \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario scenario1
```

如果要跑完整真实恢复矩阵：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model qwen3.6-plus \
  --stream-timeout 2400 \
  --event-timeout 300 \
  --preflight-timeout 60 \
  --scenario scenario1 \
  --scenario selection-waiting \
  --scenario ask-waiting \
  --scenario step1-running \
  --scenario step2-running \
  --scenario step3-running \
  --scenario step4-running \
  --scenario step5-running \
  --scenario normal-running \
  --scenario cancel-step1 \
  --scenario cancel-step2 \
  --scenario cancel-step3 \
  --scenario cancel-step4 \
  --scenario cancel-step5 \
  --scenario rollback-step1 \
  --scenario rollback-step2 \
  --scenario rollback-step3 \
  --scenario rollback-step4 \
  --scenario rollback-step5 \
  --scenario rollback-step5-cleanup \
  --scenario rollback-step5-cleanup-recovery
```

provider、tool、真实云调用场景默认会被保护住。只有确认要使用真实 provider 和阿里云凭证
时，才加 `--allow-real-cloud`。
`rollback-step5-cleanup` 这两个场景会故意保留第 2 个 stack，作为“只清理回滚残留”的验收
证据；检查完 run 产物后请再手工或通过后续流程删除它。

## 每个场景覆盖什么

`scenario1` 是历史遗留名称，表示“pipeline 完成后恢复 normal chat”的基线场景。它不是
单独 runner，也不是特殊模式，而是完整场景矩阵中的一个场景。

| 场景 | kill server 的位置 | 恢复时输入 | 主要验收 |
| --- | --- | --- | --- |
| `scenario1` | pipeline 完成并完成一轮 normal-chat follow-up 后 | 询问上一条 normal-chat 问题是什么 | normal-chat 历史重启后仍可用；存在 VSwitch 证据。 |
| `selection-waiting` | step4 等待候选方案选择时 | 不带 `taskId` 发送 `你随便选一个方案。` | 能恢复等待中的 step4 task 并完成选择；存在 VSwitch 证据。 |
| `ask-waiting` | `ask_user_question` 等待用户输入时 | 不带 `taskId` 发送澄清回答 | 能恢复 pending ask 输入并完成 pipeline；存在 VSwitch 证据。 |
| `step1-running` | `intent_parsing` 运行中 | `继续` | running pipeline task 能恢复并完成；存在 VSwitch 证据。 |
| `step2-running` | `architecture_planning` 运行中 | `继续` | running pipeline task 能恢复并完成；存在 VSwitch 证据。 |
| `step3-running` | `evaluate_candidates` 的 candidate/sub-pipeline 运行中 | `继续` | sub-pipeline 状态能恢复并完成；存在 VSwitch 证据。 |
| `step4-running` | `confirm_and_select` 运行中、尚未进入选择输入前 | `继续`，随后选择方案 | step4 running 状态能恢复到选择并完成；存在 VSwitch 证据。 |
| `step5-running` | `deploying` 运行中 | `继续` | deploying step 能恢复并完成；存在 VSwitch 证据。 |
| `normal-running` | pipeline handoff 后的 normal-chat 响应流式输出中 | `继续`，随后检查历史 | normal-chat task 恢复后仍保持同一个 `contextId` 历史。 |
| `cancel-step1` ... `cancel-step5` | 在指定 step cancel 活跃 pipeline task | cancel 后 normal-chat follow-up，重启后检查历史 | canceled snapshot 保持 canceled；normal-chat 历史重启后仍可用。 |
| `rollback-step1` ... `rollback-step5` | step3 收到回滚到 `intent_parsing`，随后在回滚后的指定 step kill | `继续`，必要时再选择方案 | 回滚后的 pipeline 以安全组任务完成，不再是 VSwitch。 |
| `rollback-step5-cleanup` | 第一次 step5 stack 已被观测后触发回滚，随后第二次 step5 创建新 stack 并进入 normal chat | normal-chat follow-up 触发 cleanup | 第 1 个回滚残留 stack 在 cleanup snapshot 中完成，且 ROS 中已删除；第 2 个 stack 仍保留。 |
| `rollback-step5-cleanup-recovery` | 基于 `rollback-step5-cleanup`，在第 1 个 stack cleanup 开始后 kill server | 重启后在 normal chat 发送 `继续` | 恢复后重新触发 cleanup；第 1 个 stack 被删除，第 2 个 stack 仍保留。 |
| `fault-after-snapshot` | A2A pipeline snapshot 持久化后确定性 crash | `继续`，必要时再选择方案 | `GetTask` / `ListTasks` 能看到恢复 task，pipeline 能完成。 |

## 代表输入

大部分场景使用同一个基线需求：

```text
选择一个已有vpc，创建一个vswitch
```

候选方案选择使用：

```text
你随便选一个方案。
```

running 状态恢复使用：

```text
继续
```

`ask-waiting` 会先用一个故意模糊的提示词触发 `ask_user_question`：

```text
我有个产品要上线
```

rollback 场景会在 step3 发送：

```text
回退到 intent_parsing，选择一个已有vpc，创建一个安全组
```

## 推荐执行顺序

稳定或回归时，建议从更小、更容易定位问题的场景开始：

1. `fault-after-snapshot`
2. `scenario1`
3. `selection-waiting`
4. `ask-waiting`
5. `step1-running` 到 `step5-running`
6. `normal-running`
7. `cancel-step1` 到 `cancel-step5`
8. `rollback-step1` 到 `rollback-step5`
9. `rollback-step5-cleanup`，再跑 `rollback-step5-cleanup-recovery`

## Preflight

真实场景默认会先跑一个极小的 normal-chat LLM preflight，除非显式传
`--skip-preflight`。也可以手动先跑：

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=normal \
IAC_CODE_PROVIDER=dashscope \
IAC_CODE_MODEL=qwen3.6-plus \
uv run iac-code --prompt '只回复 OK'
```

如果这里返回 `APIConnectionError`、`APITimeoutError` 或认证错误，需要先修复
provider、网络或凭证。否则 E2E 会在证明 A2A 会话恢复前就失败。

## 常用参数

```bash
# 把所有场景产物保存到指定根目录。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --run-root /tmp/iac-code-a2a-e2e-runs/manual

# 指定精确 run 目录。只能和单个 --scenario 一起使用。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --run-dir /tmp/iac-code-a2a-e2e-scenario1

# 使用固定 server 端口。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --port 41299

# 把 A2A 工具执行发送到指定 workspace，而不是默认 run dir。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --cwd /tmp/iac-code-a2a-e2e-workspace

# 临时覆盖本次运行使用的 model/provider，不修改 settings.yml。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --provider dashscope \
  --model qwen3.6-plus

# 结束后保留重启后的 server，便于手工排查。
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --leave-server-running
```

## 产物

除非指定 `--run-root` 或 `--run-dir`，每个场景都会写入一个独立 run 目录：

```text
/tmp/iac-code-a2a-e2e-runs/<scenario>/<timestamp>-<pid>-<suffix>/
```

关键文件：

- `summary.json`：场景结果、检查结果、`contextId`、`taskId` 和 stream 摘要。
- `requests.jsonl`：runner 发送的 JSON-RPC 请求。
- `*.events.jsonl`：每个 stream 的原始 SSE payload。
- `before-kill.pipeline-state.json`、`after-restart.pipeline-state.json` 等文件：pipeline 恢复 snapshot。
- `*.task-get.json` 和 `*.task-list.json`：场景捕获到的、经过脱敏的 `GetTask` / `ListTasks` artifact。
- `server-1.*.log` 和 `server-2.*.log`：重启前后的 server 日志。
- `a2a-server.yml`：生成的 server 配置。
- `workspace/`：默认 A2A metadata cwd；除非指定 `--cwd`，工具输出和生成模板会写到这里。
- `preflight.json`：provider preflight 结果；使用 `--skip-preflight` 时不会生成。

脚本写入产物前，会对常见 API key、token、secret、password、credential 和
authorization 值做基础脱敏。即便如此，run 目录仍应视为敏感：原始模型/工具文本仍可能
包含云资源 ID、提示词、生成模板或其他账号相关细节。

Runner 会隔离 A2A task 持久化和 A2A artifacts，并默认使用 run 目录下的 `workspace/`
作为 A2A 工具执行目录。普通 iac-code session 历史、tool-results、telemetry 和 logs
仍可能写入当前生效的 `IAC_CODE_CONFIG_DIR` 或默认 `~/.iac-code/` 目录。
