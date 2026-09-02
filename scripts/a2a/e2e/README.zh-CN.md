# A2A 会话恢复与脱敏 E2E

## 真实 StartChat 权限等待矩阵

`run_start_chat_permission_wait.py` 是本功能可重复执行、受凭证开关保护的真实链路：Qoder 真实 LLM
→ 安装后的 `alicloud-ros-agent` Skill → Python bridge → 原生 `aliyun` CLI → 只暴露 StartChat/StopChat
的本地 HTTPS relay → 本地 iac-code A2A server → 真实 iac-code LLM 和云调用。Runner 会先在调用方指定的
`--source-config-dir` 中原地刷新 OAuth STS，再把最新凭证复制到权限受限的独立 config dir。这样可避免只在
一次性副本中刷新并轮换 OAuth refresh token，导致源配置在下一个场景失效。随后把服务端策略固定为
`300 / 300 / 30`，启用共享备份提交协议，使用唯一的 Stack/VSwitch 名称，并在结束时仅按精确名称做兜底清理。

`--run-dir` 仍是日志和证据的输出根目录，可以继续使用 `/tmp/...` 等路径。runner 会自动把 Qoder 工作区和
ROS Agent manager 状态放到 Python 的 `tempfile.gettempdir()` 下，因为托管 Skill 只接受位于当前用户主目录
或 Python 所识别临时目录中的本地 manager 路径。这一点在 macOS 上尤其重要：`/tmp` 会解析为
`/private/tmp`，而 Python 通常返回另一个用户级临时目录。

Qoder 的 host 权限绕过只用于允许测试驱动执行本地 Bash/文件操作，不会批准 ROS Agent 权限。隔离的 iac-code
配置使用默认权限模式，显式允许辅助工具并要求云资源变更工具确认；A2A server 同时保持
`auto_approve_permissions: false`，非只读云操作仍必须通过带完整关联字段的 StartChat 权限回答。

每个场景使用新的目录：

真实 headless Qoder 的单轮超时默认是 900 秒，保证双 candidate Pipeline 不会被测试驱动过早终止；
provider 更慢时可通过 `--qoder-turn-timeout` 显式覆盖，不会削减 Pipeline 场景。

```bash
uv run python scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-pwait-normal-before \
  --mode normal

uv run python scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-pwait-pipeline-before \
  --mode pipeline

# resident 300 秒到期后，在 30 秒 grace 内回答。
uv run python scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-pwait-normal-grace \
  --mode normal \
  --answer-delay-seconds 305

# 等非失败挂起完成后再回答。
uv run python scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-pwait-pipeline-suspended \
  --mode pipeline \
  --answer-delay-seconds 335

# 在首个权限等待点只终止当前模式的本地 A2A 进程，用同一 config/persistence 目录重启后回答。
uv run python scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-pwait-normal-restart \
  --mode normal \
  --restart-at-first-permission
```

Normal 和 Pipeline 都要分别运行 grace、挂起后恢复和进程重启变体。仓库内 prompt 是
`permission_wait/permission_wait_start_chat_prompt.md`。每次运行只保存有界 Qoder turn 摘要、权限观察、relay metrics、
安全结果清单和本地服务日志；退出前会先提取有界只读证据，再删除复制的凭证文件和完整会话 transcript。
发现只读权限弹窗或范围外云写入时，Runner 会直接失败，
不会替用户批准。最终断言要求：真实非只读权限、Normal/顶层 Pipeline 本地与共享 checkpoint、Sub Pipeline 无
checkpoint、确实经过原生 StartChat、精确清理本次资源，以及全部原有 VPC 仍存在。

受控 Sub Pipeline fixture 使用真实 `PipelineRunner` 和两个真实 `AgentLoop` candidate：一个 candidate
停在真实权限 Future，另一个自然完成；到达配置的硬超时后，父 Pipeline 聚合两个 conclusion、进入 candidate
选择并自然完成。fixture 同时安装生产 A2A 备份 hook，证明 Sub 权限本身不会触发权限关键备份。验收运行使用
生产环境的 300 秒配置：

```bash
uv run python scripts/a2a/e2e/permission_wait/run_sub_pipeline_permission_timeout.py \
  --run-dir /tmp/iac-pwait-sub-pipeline-300 \
  --timeout-seconds 300
```

对应的加速回归和无真实凭证的快速进程重启矩阵为：

```bash
uv run pytest -q tests/a2a_e2e/test_sub_pipeline_permission_timeout.py
uv run pytest -q tests/a2a_e2e/test_permission_wait_restart.py
```

其中 `staged-backup-generation-fence` 场景使用真实 HTTP A2A 进程、生产 staged backup/worker 和两个连续
权限点：新进程先从共享目录恢复旧 generation；较新的权限 backup 尚未发布时，Resume 必须返回可重试的
`SESSION_BACKUP_NOT_READY`；worker 发布完成后，同一标准权限响应可恢复最新会话并且两个工具各执行一次。
可单独运行：

```bash
uv run python scripts/a2a/e2e/permission_wait/run_permission_wait_restart.py \
  --run-dir /tmp/iac-pwait-generation-fence \
  --decision allow_once \
  --staged-backup-generation-fence
```

进程重启矩阵覆盖 Normal/Pipeline × allow/deny。Sub Pipeline fixture 断言只生成一次拒绝 ToolResult、
Agent loop 继续、父 Pipeline 进入 candidate 选择并完成，且全程没有 grace、持久化 permission checkpoint
或权限关键备份。

同一套重启测试还覆盖 `selling_solution_first` 的三个步骤
（`solution_planning_and_selection`、`materialize_selected_candidate`、`deploying`）以及
Pipeline handoff 后的普通对话。每个作用域都分别执行 `allow_once` 和 `deny`，严格只触发一次权限，
在回答前重启真实 HTTP A2A server，并校验安全 operation/参数投影。fixture 只执行确定性的本地工具，
不会产生真实云写操作。

本目录包含用于 A2A pipeline 会话恢复和脱敏回归的 headless 端到端检查。Runner 会驱动公开的
A2A JSON-RPC streaming endpoint 并记录 SSE 事件和 pipeline snapshot。恢复场景会用 `SIGKILL`
杀掉 A2A server，再用相同持久化目录重启；`redaction-step4` 则在候选方案选择处停止，不重启、
不提交选择，也不进入部署。

脚本流程刻意贴近 `scripts/a2a/debugger.py` 的手工 Web debugger：`contextId` 表示
一次会话，`taskId` 表示这个会话里的一个 A2A task。

## 快速开始

从仓库根目录运行：

```bash
cd /path/to/iac-code
```

真实场景会按输入类型自动选择模型：非多模态场景使用 `deepseek-v4-flash-0731`，所有
`image-*` 场景使用 `qwen3.8-max`。一次命令混跑文本和图片场景时也会逐场景选择；显式传入
`--model` 会覆盖本次命令中的所有场景。

### Aliyun 结果契约与 telemetry 场景

以下 wrapper 复用本目录真实 A2A server、JSON-RPC/SSE client 和 session persistence，但使用确定性
LLM、Aliyun transport 与 ROS stack fixture。默认一次执行三条场景：

- `e3a-recovery`：pipeline snapshot 后 `SIGKILL`，重启并继续同一 task/context。
- `e3b-success`：完整 graceful success，校验 Aliyun business body、公开出口和 telemetry 归属。
- `e3b-cancel`：流式 provider attempt 进行中取消 task，校验唯一 failed terminal。

```bash
uv run python scripts/a2a/e2e/run_contract_scenarios.py \
  --run-dir /tmp/iac-code-contract-e3
```

可重复传 `--scenario` 只运行指定场景。每个子目录都会生成 `contract-summary.json`、
`aliyun-contract-audit.json`（非 cancel 场景）、`public-payload-audit.json`、`telemetry-audit.json`、
provider/A2A attribution audit，以及原有恢复 runner 的 task、event 和 snapshot 产物。

如果只想做最短恢复检查，先跑 deterministic crash smoke。它会在 A2A pipeline
snapshot 保存后注入一次 crash，然后重启 server，并验证 task 恢复 artifact：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --deterministic \
  --provider dashscope \
  --scenario fault-after-snapshot \
  --fault-at after_a2a_pipeline_snapshot_saved
```

deterministic 模式只固定 crash 注入点；重启后的 pipeline 仍会使用真实 provider、
工具和云 API。因此除非已单独验证 provider，否则不要跳过 preflight。

如果想快速验证一条真实 provider / tool / 云路径，跑一个代表场景：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario scenario1
```

验证 iac-code Web 的 2 vCPU / 4 GiB 需求时，使用固定用户原文运行到 step 4；场景不会提交候选方案，
也不会进入 `deploying` 或调用 `ros_deploy`。它会把 canonical pipeline evidence 中的 golden solution
、结构化 CPU/内存约束，以及 PreviewStack 在询价前被调用写入结果检查：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario iac-code-web-2c4g-step4
```

如果要复现资源栈参数被错误替换为 `***` / `[REDACTED]` 的问题，运行真实的 step 4
脱敏回归场景：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario redaction-step4
```

该场景使用真实的小程序后端 + 数据库 + 月预算 200 元 + 2 个方案需求，并明确要求两个 ROS 方案
都创建数据库主账号、生成 NoEcho 密码参数，以稳定命中原始问题；server 强制使用
`IAC_CODE_A2A_SAFE_MODE=true`。它只运行到 `confirm_and_select` 的两个候选方案展示，然后在内存中比较
canonical snapshot 与 A2A recovery response：生成的密码参数必须保持同一真实值，snapshot 中存在的
token 统计必须仍为数字，只有已知服务器路径可以变成 `[PATH]`。`redaction-audit.json` 只记录字段路径、计数和布尔结果，
不会写入密码值。可以用 `--redaction-step4-prompt` 临时覆盖需求，但这样可能无法再保证生成密码参数。

如果要验证和生产性能/backup 配置一致的 scenario1 变体，并在 step4 选择时不带
`taskId`，运行：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --preflight-timeout 60 \
  --scenario scenario1-performance-backup
```

如果要复现“候选选择页已出现，但 `input_required` backup 仍在慢速 OSS 上执行”的竞态，运行：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --event-timeout 300 \
  --preflight-timeout 60 \
  --scenario selection-during-backup
```

该场景会在首轮请求前 arm 仅注入 server 子进程的 E2E fixture，把 step4 `input_required` backup 至少阻塞 10 秒；
runner 收到 backup started 标记后立即提交方案，同时继续核验候选选择事件。场景会验证选择请求确实在 backup 的 started/finished 窗口内发出、
最终被消费为 candidate selection，并且没有产生 `interrupt_received` / `interrupt_classified`。

如果要跑完整真实 E2E 矩阵：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/e2e/run_recovery_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --stream-timeout 2400 \
  --event-timeout 300 \
  --preflight-timeout 60 \
  --scenario scenario1 \
  --scenario scenario1-performance-backup \
  --scenario selection-during-backup \
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

provider、tool、真实云调用场景默认会被保护住。只有确认要使用真实 provider 和阿里云凭证
时，才加 `--allow-real-cloud`。
`rollback-step5-cleanup` 这两个场景会故意保留第 2 个 stack，作为“只清理回滚残留”的验收
证据；检查完 run 产物后请再手工或通过后续流程删除它。

## 每个场景覆盖什么

`scenario1` 是历史遗留名称，表示“pipeline 完成后恢复 normal chat”的基线场景。它不是
单独 runner，也不是特殊模式，而是完整场景矩阵中的一个场景。

| 场景 | 切点 / 特殊条件 | 后续输入 / 恢复输入 | 主要验收 |
| --- | --- | --- | --- |
| `redaction-step4` | 强制 A2A safe mode，真实小程序后端/数据库需求到达 step4 候选方案选择即停止 | 无；不提交方案选择 | canonical 密码参数不是脱敏占位符；A2A 密码值与 canonical 一致；存在的 token 统计仍为数字；已知服务器路径只在 A2A 副本中变成 `[PATH]`；不进入部署。 |
| `scenario1` | pipeline 完成并完成一轮 normal-chat follow-up 后 | 询问上一条 normal-chat 问题是什么 | normal-chat 历史重启后仍可用；存在 VSwitch 证据。 |
| `scenario1-performance-backup` | 完整 `scenario1`，并强制 `IAC_CODE_A2A_EXTREME_PERFORMANCE=true`、`IAC_CODE_CONFIG_BACKUP_DIR=<run-dir>/session-backup` | step4 backup 落盘后停服并删除主 `projects` 下对应 session，重启后不带 `taskId` 选择；后续 normal-chat 恢复也不带 `taskId` | 重启前只有 backup session；重启本身不会重建主 session；省略 `taskId` 的选择会从 backup restore 主 session 并 hydrate 到恢复 task；完整 scenario1 通过。 |
| `selection-during-backup` | 首轮请求前 arm E2E fixture；step4 `input_required` backup 开始后至少阻塞 10 秒并写出 started 标记 | backup 仍在执行时，携带原 task 的 `taskId` 立即发送 `你随便选一个方案。`，随后核验 step4 事件 | 请求时间落在 backup started/finished 窗口内；选择被排队并作为 candidate input 消费；不产生 interrupt 事件；pipeline 完成。 |
| `selection-waiting` | step4 等待候选方案选择时 | 不带 `taskId` 发送 `你随便选一个方案。` | 能恢复等待中的 step4 task 并完成选择；存在 VSwitch 证据。 |
| `ask-waiting` | `ask_user_question` 等待用户输入时 | 不带 `taskId` 发送澄清回答 | 能恢复 pending ask 输入并完成 pipeline；存在 VSwitch 证据。 |
| `image-initial` | 首轮用户消息就是静态 `initial.png` 图片 fixture | 文本选择候选方案 | 图片能启动 pipeline，进入 step4 选择，最终完成并产生 VSwitch 证据。 |
| `image-ask-waiting` | `ask_user_question` 等待用户输入，随后重启 server | 不带 `taskId` 发送静态 `ask-first-answer.png` / `ask-second-answer.png` 图片 fixture | pending ask 输入能恢复，图片回答能 hydrate 到恢复后的 task，最终完成并产生 VSwitch 证据。 |
| `image-selection-waiting` | step4 等待候选方案选择，随后重启 server | 不带 `taskId` 发送静态 `selection.png` 图片 fixture | 能恢复等待中的 step4 task，图片选择被接受，并产生 VSwitch 证据。 |
| `image-normal-handoff` | pipeline 完成并 handoff 到 normal chat；normal follow-up 是静态 `normal-followup.png`，随后重启 server | 不带 `taskId` 发送 normal-chat 恢复问题 | 图片 follow-up 保持同一个 `contextId`，使用新的 normal-chat task；completed handoff 状态重启后仍可恢复。 |
| `image-interrupt` | step3 收到静态 `rollback-interrupt.png` 图片，表示回滚到 `intent_parsing`，随后重启 server | `继续`，必要时再选择方案 | 图片 interrupt 能被识别；pipeline 以安全组任务完成，最终部署证据不是 VSwitch。 |
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

`redaction-step4` 使用原始问题对应的真实回归需求：

```text
帮我在阿里云上搭个小程序后端环境，要数据库，平时访问不多，每月最好别超过 200 块。2个方案。两个方案的 ROS 模板都要创建数据库主账号，并为主账号定义 NoEcho 密码参数；密码由你生成满足约束的随机值，带入预览并完整保留到方案选择，不要让我提供。
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

图片场景会发送一个很短的读图提示词，并附带
`scripts/a2a/e2e/fixtures/text-images/` 下的静态 PNG fixture。fixture manifest 会固化
文本、文件名、媒体类型、字节数和 SHA-256。每次场景运行还会写
`image-fixtures/manifest.json`；固定 prompt 应显示 `source: static`。只有临时输入或通过
CLI 覆盖后的文本，才会回退到运行时渲染图片。

## 推荐执行顺序

稳定或回归时，建议从更小、更容易定位问题的场景开始：

1. `redaction-step4`
2. `fault-after-snapshot`
3. `scenario1`
4. `scenario1-performance-backup`
5. `selection-during-backup`
6. `selection-waiting`
7. `ask-waiting`
8. `image-initial`、`image-ask-waiting` 和 `image-selection-waiting`
9. `image-normal-handoff` 和 `image-interrupt`
10. `step1-running` 到 `step5-running`
11. `normal-running`
12. `cancel-step1` 到 `cancel-step5`
13. `rollback-step1` 到 `rollback-step5`
14. `rollback-step5-cleanup`，再跑 `rollback-step5-cleanup-recovery`

## Preflight

真实场景默认会先跑一个极小的 normal-chat LLM preflight，除非显式传
`--skip-preflight`。也可以手动先跑：

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=normal \
IAC_CODE_PROVIDER=dashscope \
IAC_CODE_MODEL=deepseek-v4-flash-0731 \
uv run iac-code --prompt '只回复 OK'
```

上面的命令对应非多模态场景；检查图片场景时将模型改为 `qwen3.8-max`。

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
  --model deepseek-v4-flash-0731

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
- `*.events.jsonl`：每个 stream 经过 runner 基础脱敏后落盘的 SSE payload。
- `redaction-audit.json`：仅 `redaction-step4` 生成；记录 canonical/A2A 对比的非敏感证据，不含密码值。
- `before-kill.pipeline-state.json`、`after-restart.pipeline-state.json` 等文件：pipeline 恢复 snapshot。
- `*.task-get.json` 和 `*.task-list.json`：场景捕获到的、经过脱敏的 `GetTask` / `ListTasks` artifact。
- `server-1.*.log` 和 `server-2.*.log`：重启前后的 server 日志。
- `a2a-server.yml`：生成的 server 配置。
- `image-fixtures/manifest.json`：图片场景的图片输入 fixture 使用情况，包括每张图来自仓库静态 fixture 还是运行时渲染。
- `workspace/`：默认 A2A metadata cwd；除非指定 `--cwd`，工具输出和生成模板会写到这里。
- `preflight.json`：provider preflight 结果；使用 `--skip-preflight` 时不会生成。

脚本写入产物前，会对常见 API key、token、secret、password、credential 和
authorization 值做基础脱敏。即便如此，run 目录仍应视为敏感：原始模型/工具文本仍可能
包含云资源 ID、提示词、生成模板或其他账号相关细节。

Runner 会隔离 A2A task 持久化和 A2A artifacts，并默认使用 run 目录下的 `workspace/`
作为 A2A 工具执行目录。普通 iac-code session 历史、tool-results、telemetry 和 logs
仍可能写入当前生效的 `IAC_CODE_CONFIG_DIR` 或默认 `~/.iac-code/` 目录。
