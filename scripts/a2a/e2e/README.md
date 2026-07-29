# A2A E2E Session Recovery and Redaction

This directory contains headless end-to-end checks for A2A pipeline session
recovery and redaction regressions. The runner drives the public A2A JSON-RPC
streaming endpoint and records SSE events and pipeline snapshots. Recovery
scenarios kill and restart the A2A server. `redaction-step4` instead stops at
candidate selection without restarting, submitting a selection, or deploying.

The script is intentionally close to the manual web debugger flow in
`scripts/a2a/debugger.py`: `contextId` identifies the conversation, and `taskId`
identifies one A2A task in that conversation.

## Quick Start

Run commands from the repository root:

```bash
cd /path/to/iac-code
```

Use this deterministic crash smoke when you want the shortest recovery check.
It injects a crash after the A2A pipeline snapshot is saved, then restarts the
server and verifies task recovery artifacts:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --deterministic \
  --skip-preflight \
  --scenario fault-after-snapshot \
  --fault-at after_a2a_pipeline_snapshot_saved
```

Run one representative real scenario when you want a quick provider/tool/cloud
path check:

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

Run the real step 4 redaction regression when checking that stack parameters
are not replaced with `***` or `[REDACTED]`:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model qwen3.6-plus \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario redaction-step4
```

This scenario uses the real mini-app backend/database regression prompt and
explicitly requires both ROS candidates to create a database account with a
generated NoEcho password parameter, making the original failure deterministic. It forces
`IAC_CODE_A2A_SAFE_MODE=true`, and stops when two candidates are shown at
`confirm_and_select`. It compares the canonical snapshot with the public A2A
recovery response in memory: generated password parameters must retain the same
real value, token counters must remain numeric when present, and only known server paths may
become `[PATH]`. `redaction-audit.json` contains paths, counts, and booleans but
never credential values. `--redaction-step4-prompt` can override the task, but a
different task may not generate a password parameter.

Run the scenario1 variant that matches the production performance/backup
configuration and sends the step4 selection without `taskId`:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model qwen3.6-plus \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario scenario1-performance-backup
```

Run the full real E2E matrix:

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
  --scenario scenario1-performance-backup \
  --scenario redaction-step4 \
  --scenario selection-waiting \
  --scenario ask-waiting \
  --scenario image-initial \
  --scenario image-ask-waiting \
  --scenario image-selection-waiting \
  --scenario image-normal-handoff \
  --scenario image-interrupt \
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

Provider, tool, and cloud execution scenarios are guarded by default. Use
`--allow-real-cloud` only when you intentionally want to run against real
providers and Alibaba Cloud credentials.
The rollback step5 cleanup scenarios intentionally leave the second stack in
ROS as proof that cleanup only removed the rollback leftover; delete that stack
after you finish inspecting the run.

## What Each Scenario Covers

`scenario1` is the historical name for the completed-pipeline baseline. It is
not a separate runner or a special mode; it lives in the same scenario matrix as
the rest of the tests.

| Scenario | Cut point / special condition | Follow-up / recovery input | Main assertion |
| --- | --- | --- | --- |
| `redaction-step4` | Force A2A safe mode and stop when the real mini-app backend/database task reaches step 4 candidate selection | None; no candidate selection is submitted | Canonical password parameters are not placeholders; public A2A passwords equal canonical values; token counters stay numeric when present; known server paths become `[PATH]` only in the public copy; deployment never starts. |
| `scenario1` | After pipeline completion and one normal-chat follow-up | Ask what the previous normal-chat question was | Normal-chat history survives restart; VSwitch evidence exists. |
| `scenario1-performance-backup` | Full `scenario1` with `IAC_CODE_A2A_EXTREME_PERFORMANCE=true` and `IAC_CODE_CONFIG_BACKUP_DIR=<run-dir>/session-backup` | After the Step4 backup is durable, stop the server, remove the matching primary session under `projects`, restart, and select without `taskId`; later normal-chat recovery also omits `taskId` | Only the backup session exists before restart; restart alone does not recreate the primary session; selection without `taskId` restores the primary session from backup and hydrates the recovered task; full scenario1 passes. |
| `selection-waiting` | Step 4 waits for candidate selection | `你随便选一个方案。` without `taskId` | Waiting step4 task is recovered and selected; VSwitch evidence exists. |
| `ask-waiting` | `ask_user_question` waits for user input | Clarification answers without `taskId` | Pending ask input is recovered and pipeline completes; VSwitch evidence exists. |
| `image-initial` | Initial user message is the static `initial.png` image fixture | Candidate selection text | The image starts the pipeline, reaches step4 selection, completes, and produces VSwitch evidence. |
| `image-ask-waiting` | `ask_user_question` waits for user input, then the server restarts | Static `ask-first-answer.png` / `ask-second-answer.png` image fixtures without `taskId` | Pending ask input is recovered, image answers hydrate the recovered task, and the pipeline completes with VSwitch evidence. |
| `image-selection-waiting` | Step 4 waits for candidate selection, then the server restarts | Static `selection.png` image fixture without `taskId` | Waiting step4 task is recovered, the image selection is accepted, and VSwitch evidence exists. |
| `image-normal-handoff` | Pipeline completes and hands off to normal chat; the normal follow-up is static `normal-followup.png`, then the server restarts | Normal-chat recovery question without `taskId` | Image follow-up stays in the same `contextId`, uses a new normal-chat task, and completed handoff state survives restart. |
| `image-interrupt` | Step 3 receives static `rollback-interrupt.png` as an image rollback to `intent_parsing`, then the server restarts | `继续`, plus selection when needed | The image interrupt is recognized, the pipeline completes as a security-group task, and final deployment evidence is not VSwitch. |
| `step1-running` | `intent_parsing` running | `继续` | Running pipeline task is recovered and completes; VSwitch evidence exists. |
| `step2-running` | `architecture_planning` running | `继续` | Running pipeline task is recovered and completes; VSwitch evidence exists. |
| `step3-running` | `evaluate_candidates` candidate/sub-pipeline running | `继续` | Sub-pipeline state is recovered and completes; VSwitch evidence exists. |
| `step4-running` | `confirm_and_select` running before selection input | `继续`, then select | Step4 running state reaches selection and completes; VSwitch evidence exists. |
| `step5-running` | `deploying` running | `继续` | Deploying step recovers and completes; VSwitch evidence exists. |
| `normal-running` | Normal-chat response streaming after pipeline handoff | `继续`, then history check | Normal-chat task recovery keeps same `contextId` history. |
| `cancel-step1` ... `cancel-step5` | Active pipeline task is canceled at the named step | Normal-chat follow-up after cancel, then restart and history check | Canceled snapshot stays canceled; normal-chat history survives restart. |
| `rollback-step1` ... `rollback-step5` | Step 3 receives rollback to `intent_parsing`, then the named post-rollback step is killed | `继续`, plus selection when needed | Post-rollback pipeline completes as a security-group task, not VSwitch. |
| `rollback-step5-cleanup` | First step5 stack is observed, then rollback creates a second stack and hands off to normal chat | A normal-chat follow-up triggers cleanup | First rollback stack reaches cleanup complete and is deleted in ROS; second stack remains. |
| `rollback-step5-cleanup-recovery` | Same as `rollback-step5-cleanup`, then the server is killed after cleanup starts | `继续` in normal chat after restart | Cleanup is triggered again after restart; first stack is deleted and second stack remains. |
| `fault-after-snapshot` | Deterministic crash after A2A pipeline snapshot persistence | `继续`, plus selection when needed | `GetTask` / `ListTasks` expose the recovered task and the pipeline completes. |

## Representative Inputs

Most scenarios use the same baseline task:

```text
选择一个已有vpc，创建一个vswitch
```

`redaction-step4` uses the real regression task:

```text
帮我在阿里云上搭个小程序后端环境，要数据库，平时访问不多，每月最好别超过 200 块。2个方案。两个方案的 ROS 模板都要创建数据库主账号，并为主账号定义 NoEcho 密码参数；密码由你生成满足约束的随机值，带入预览并完整保留到方案选择，不要让我提供。
```

Candidate selection uses:

```text
你随便选一个方案。
```

Running-state recovery uses:

```text
继续
```

`ask-waiting` starts with a deliberately vague prompt to force
`ask_user_question`:

```text
我有个产品要上线
```

Rollback scenarios interrupt step 3 with:

```text
回退到 intent_parsing，选择一个已有vpc，创建一个安全组
```

Image scenarios send a small text prompt plus static PNG fixtures from
`scripts/a2a/e2e/fixtures/text-images/`. The fixture manifest pins the text,
file name, media type, byte size, and SHA-256 hash. A scenario run also writes
`image-fixtures/manifest.json`; fixed prompts should show `source: static`.
Only ad-hoc or CLI-overridden text falls back to runtime image rendering.

## Recommended Order

When stabilizing changes, run the smaller or more diagnostic cases first:

1. `redaction-step4`
2. `fault-after-snapshot`
3. `scenario1`
4. `scenario1-performance-backup`
5. `selection-waiting`
6. `ask-waiting`
7. `image-initial`, `image-ask-waiting`, and `image-selection-waiting`
8. `image-normal-handoff` and `image-interrupt`
9. `step1-running` through `step5-running`
10. `normal-running`
11. `cancel-step1` through `cancel-step5`
12. `rollback-step1` through `rollback-step5`
13. `rollback-step5-cleanup`, then `rollback-step5-cleanup-recovery`

## Preflight

The runner performs a tiny normal-chat LLM preflight before real scenarios
unless `--skip-preflight` is set. You can run the same check manually:

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=normal \
IAC_CODE_PROVIDER=dashscope \
IAC_CODE_MODEL=qwen3.6-plus \
uv run iac-code --prompt '只回复 OK'
```

If this returns `APIConnectionError`, `APITimeoutError`, or an authentication
error, fix provider, network, or credentials first. Otherwise the E2E run will
fail before it can prove A2A recovery.

## Useful Options

```bash
# Keep all scenario artifacts under a known root.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --run-root /tmp/iac-code-a2a-e2e-runs/manual

# Use an exact run directory. This is only valid with one --scenario.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --run-dir /tmp/iac-code-a2a-e2e-scenario1

# Use a fixed server port.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --port 41299

# Send A2A tool execution to a specific workspace instead of the default run dir.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --cwd /tmp/iac-code-a2a-e2e-workspace

# Temporarily override model/provider without changing settings.yml.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --provider dashscope \
  --model qwen3.6-plus

# Keep the restarted server running for manual inspection.
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --leave-server-running
```

## Artifacts

Unless `--run-root` or `--run-dir` is provided, each scenario writes a standalone
run directory under:

```text
/tmp/iac-code-a2a-e2e-runs/<scenario>/<timestamp>-<pid>-<suffix>/
```

Important files:

- `summary.json`: scenario result, check results, `contextId`, `taskId`, and stream summaries.
- `requests.jsonl`: JSON-RPC requests sent by the runner.
- `*.events.jsonl`: SSE payloads after the runner's basic artifact redaction.
- `redaction-audit.json`: non-sensitive canonical/A2A comparison evidence from `redaction-step4`; it contains no password values.
- `before-kill.pipeline-state.json`, `after-restart.pipeline-state.json`, and similar files: pipeline recovery snapshots.
- `*.task-get.json` and `*.task-list.json`: redacted `GetTask` / `ListTasks` artifacts when captured by the scenario.
- `server-1.*.log` and `server-2.*.log`: server logs before and after restart.
- `a2a-server.yml`: generated server config.
- `image-fixtures/manifest.json`: image input fixture usage for image scenarios, including whether each image came from a static repository fixture or runtime rendering.
- `workspace/`: default A2A metadata cwd and generated tool outputs unless `--cwd` is provided.
- `preflight.json`: provider preflight result unless `--skip-preflight` is used.

The runner applies basic redaction for common API key, token, secret, password,
credential, and authorization values before writing artifacts. Treat the run
directory as sensitive anyway: raw model/tool text can still contain cloud
resource identifiers, prompts, generated templates, or other account-specific
details.

The runner isolates A2A task persistence and A2A artifacts, and by default uses
`workspace/` under the run directory for A2A tool execution. Normal iac-code
session history, tool-results, telemetry, and logs may still be written under
the active `IAC_CODE_CONFIG_DIR` or the default `~/.iac-code/` tree.
