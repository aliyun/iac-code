# REPL Pipeline E2E

本目录包含通过真实交互式终端回归 pipeline 功能的脚本。它和
`scripts/a2a/e2e/run_recovery_scenarios.py` 目标相同，都是回归 pipeline；区别是这里走真实
REPL / PTY 入口，而 A2A runner 走 JSON-RPC / SSE 入口。

## 重要说明

- 默认使用当前用户真实 `~/.iac-code` 配置；真实场景的模型由 runner 按下述规则覆盖。
- 会调用真实 LLM provider。
- 带 `--allow-real-cloud` 的 pipeline 场景可能调用真实阿里云工具和凭证。
- 不属于普通 `make test`，也不会在 pytest 中执行真实场景。
- 该 runner 通过 `pexpect` 使用真实 PTY，仅支持 POSIX 环境；Windows 会提前报错，不作为本脚本支持目标。

非多模态场景默认使用 `deepseek-v4-flash-0731`，所有 `image-*` 场景默认使用
`qwen3.8-max`。一次命令混跑文本和图片场景时会逐场景选择模型；显式传入 `--model` 会覆盖
本次命令中的所有场景。E5 只读 canary 属于非多模态场景，也默认使用
`deepseek-v4-flash-0731`；E1/E2 的确定性 provider fixture 不受此规则影响。

## Aliyun 结果契约与 telemetry 回归

本目录额外包含三条针对 Aliyun business body 和 provider stream telemetry 的回归：

- E1：真实 REPL/PTY normal 会话、`aliyun_api` 调用、退出后 `--continue` 恢复；provider 和 Aliyun transport 使用确定性 fixture。
- E2：真实 REPL/PTY selling pipeline、delegated `ValidateTemplate`、候选选择等待点和恢复；provider 与 Aliyun transport 使用确定性 fixture。
- E5：真实 REPL/PTY、当前已配置的真实 LLM provider 和真实阿里云 VPC `DescribeVpcs` 只读调用。

```bash
# E1：normal + resume
uv run python scripts/repl/e2e/run_contract_scenarios.py \
  --run-dir /tmp/iac-code-contract-e1

# E2：selling pipeline + resume
uv run python scripts/repl/e2e/run_pipeline_contract_scenario.py \
  --run-dir /tmp/iac-code-contract-e2

# E5：真实 LLM + 真实阿里云只读 canary
uv run python scripts/repl/e2e/run_real_aliyun_contract_canary.py \
  --allow-real-cloud \
  --run-dir /tmp/iac-code-contract-e5
```

E5 会把当前配置目录中的 `.credentials.yml`、`.cloud-credentials.yml` 和 `settings.yml` 复制到权限为
`0600` 的隔离目录，不打印凭证；验收要求模型恰好调用一次
`Vpc/2016-04-28/DescribeVpcs`，且参数只能是 `PageSize=10`。它不会创建、修改或删除云资源，未显式传
`--allow-real-cloud` 时会直接拒绝运行。

三条场景都校验：持久化工具结果是业务对象、`aliyun_http` 只存在于内部 metadata、恢复和公开输出不泄漏
metadata，以及每个 provider attempt 恰有一个 started 和一个 terminal。各 run 目录中的
`summary.json`、`aliyun-contract-audit.json`、`telemetry-audit.json` 和 transcript 是最终证据。

## A2A 临时备份并发同步 E2E

该场景验证 `IAC_CODE_CONFIG_BACKUP_TMP_DIR`：A2A 请求先阻塞地把会话复制到本地临时目录，单个后台
进程再把不同会话并发同步到最终备份目录；同一会话的多个版本仍按版本号串行。临时备份只在
`iac-code a2a` 中启用，因此这个用例虽然记录在本目录，但不会通过 REPL runner 启动。

前置条件：当前用户已配置可用的真实 LLM provider；`IAC_CODE_E2E_OSS_DIR` 必须指向一个专用于本轮测试、
初始为空且支持 POSIX 权限的 OSS 挂载目录。不要指向已有备份目录，也不要使用 root 用户运行。

先准备隔离目录并启动真实 A2A Server：

```bash
set -euo pipefail
: "${IAC_CODE_E2E_OSS_DIR:?请设置为本轮测试专用的空 OSS 挂载目录}"

export E2E_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/iac-code-backup-staging-e2e.XXXXXX")"
export BACKUP_TMP="$E2E_ROOT/backup-tmp"
export BACKUP_OSS="$(cd "$IAC_CODE_E2E_OSS_DIR" && pwd)"
export A2A_URL="http://127.0.0.1:41249"
mkdir -p "$E2E_ROOT/config" "$E2E_ROOT/workspace" "$BACKUP_TMP"

if find "$BACKUP_OSS" -mindepth 1 -print -quit | grep -q .; then
  echo "IAC_CODE_E2E_OSS_DIR 必须为空" >&2
  exit 1
fi

for name in .credentials.yml .cloud-credentials.yml settings.yml; do
  if [ -f "$HOME/.iac-code/$name" ]; then
    cp "$HOME/.iac-code/$name" "$E2E_ROOT/config/$name"
    chmod 600 "$E2E_ROOT/config/$name"
  fi
done

# 先临时关闭最终目录的写权限，确定性地让三个完整版本都留在本地待同步队列中。
chmod 500 "$BACKUP_OSS"
if mkdir "$BACKUP_OSS/permission-probe" 2>/dev/null; then
  rmdir "$BACKUP_OSS/permission-probe"
  echo "OSS 挂载未执行 POSIX 写权限，本测试无法建立同步闸门" >&2
  exit 1
fi

cleanup_backup_staging_e2e() {
  chmod 700 "$BACKUP_OSS" 2>/dev/null || true
  if [ -n "${SERVER_PID:-}" ]; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup_backup_staging_e2e EXIT

IAC_CODE_MODE=normal \
IAC_CODE_CONFIG_DIR="$E2E_ROOT/config" \
IAC_CODE_CONFIG_BACKUP_DIR="$BACKUP_OSS" \
IAC_CODE_CONFIG_BACKUP_TMP_DIR="$BACKUP_TMP" \
IACCODE_A2A_ALLOWED_CWDS="$E2E_ROOT/workspace" \
uv run iac-code a2a \
  --host 127.0.0.1 \
  --port 41249 \
  --debug \
  --log-to-stdout \
  >"$E2E_ROOT/server.log" 2>&1 &
SERVER_PID=$!

uv run python - <<'PY'
import os
from scripts.a2a.e2e.common import wait_for_server

wait_for_server(os.environ["A2A_URL"], timeout=60)
PY
```

在同一个 workspace 中同时发起两个不同会话，再给会话 A 发送第二轮消息。前两次调用必须并发，第三次调用
必须复用会话 A 的 `contextId` 和 `taskId`：

```bash
uv run python - <<'PY'
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from scripts.a2a.e2e.common import stream_message

root = Path(os.environ["E2E_ROOT"])
tmp = Path(os.environ["BACKUP_TMP"])
url = os.environ["A2A_URL"]

def call(name: str, workspace: str, prompt: str, context_id: str = "", task_id: str = ""):
    return stream_message(
        server_url=url,
        cwd=str(root / workspace),
        prompt=prompt,
        name=name,
        run_dir=root,
        timeout=120,
        context_id=context_id,
        task_id=task_id,
    )

with ThreadPoolExecutor(max_workers=2) as executor:
    future_a = executor.submit(call, "session-a-v1", "workspace", "只回复 A，不调用工具。")
    future_b = executor.submit(call, "session-b-v1", "workspace", "只回复 B，不调用工具。")
    first_a = future_a.result()
    first_b = future_b.result()

assert first_a.context_id and first_a.task_id
assert first_b.context_id and first_b.task_id
assert first_a.context_id != first_b.context_id
second_a = call(
    "session-a-v2",
    "workspace",
    "只回复 A2，不调用工具。",
    first_a.context_id,
    first_a.task_id,
)

snapshots = sorted(
    path for path in tmp.glob("projects/*/*")
    if path.is_dir() and re.search(r"_v[1-9][0-9]*$", path.name)
)
names = [path.name for path in snapshots]
assert len(snapshots) == 3, names
assert sum(name.endswith("_v1") for name in names) == 2, names
assert sum(name.endswith("_v2") for name in names) == 1, names
assert not list(tmp.rglob("*.copying"))

(root / "client-summary.json").write_text(
    json.dumps(
        {"sessionAV1": asdict(first_a), "sessionBV1": asdict(first_b), "sessionAV2": asdict(second_a)},
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
PY
```

此时三次请求都已成功，但最终目录仍不可写，因此三个版本必须完整保留在 `BACKUP_TMP`。打开 OSS 写权限，
等待后台进程完成同步，再执行自动验收：

```bash
chmod 700 "$BACKUP_OSS"

uv run python - <<'PY'
import json
import os
import time
from pathlib import Path

tmp = Path(os.environ["BACKUP_TMP"])
backup = Path(os.environ["BACKUP_OSS"])
deadline = time.monotonic() + 300
while any(tmp.iterdir()) and time.monotonic() < deadline:
    time.sleep(0.2)

assert not any(tmp.iterdir()), "临时备份目录未在 300 秒内清空"
states = [json.loads(path.read_text(encoding="utf-8")) for path in backup.glob("projects/*/*/.backup-state.json")]
assert len(states) == 2, states
assert len({path.parent.parent.name for path in backup.glob("projects/*/*/.backup-state.json")}) == 1
assert sorted(state["generation"] for state in states) == [1, 2], states
assert all(state["status"] == "succeeded" for state in states), states

lines = (Path(os.environ["E2E_ROOT"]) / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()
active_starts = {}
starts = {}
finishes = {}
for index, line in enumerate(lines):
    for marker in ("Publishing", "Published"):
        token = f"{marker} staged session backup session_id="
        if token not in line:
            continue
        tail = line.split(token, 1)[1]
        session_id, generation_text = tail.split(" generation=", 1)
        key = (session_id, int(generation_text.split()[0]))
        if marker == "Publishing":
            active_starts[key] = index
        else:
            starts[key] = active_starts[key]
            finishes[key] = index

generation_two = next(state for state in states if state["generation"] == 2)
session_a = generation_two["session_id"]
session_b = next(state["session_id"] for state in states if state["session_id"] != session_a)
assert max(starts[(session_a, 1)], starts[(session_b, 1)]) < min(
    finishes[(session_a, 1)], finishes[(session_b, 1)]
), "两个会话没有形成并发同步窗口"
assert finishes[(session_a, 1)] < starts[(session_a, 2)], "同一会话的 v2 早于 v1 完成"

print("PASS: A2A 本地临时备份、跨会话并发同步、同会话版本顺序均符合预期")
PY
```

验收证据包括 `$E2E_ROOT/client-summary.json`、三份 `*.events.jsonl`、`server.log` 和最终 OSS 中两个会话的
`.backup-state.json`。失败条件是：OSS 不可写时请求失败、临时目录不是两个 `v1` 加一个 `v2`、出现
`.copying`、开放 OSS 后临时目录未清空、最终 generation 不是 `[1, 2]`、两个会话没有同步重叠，或同一
会话在 `v1` 完成前开始同步 `v2`。

## 快速开始

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1
```

指定 provider 并显式覆盖 model，但不写入 `settings.yml`：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model deepseek-v4-flash-0731 \
  --scenario scenario1
```

## 场景

| 场景 | 覆盖 |
| --- | --- |
| `scenario1` | 通过 REPL 完成 VSwitch pipeline、候选方案选择、handoff normal chat |
| `ask-waiting` | 通过 REPL 回复澄清问题后继续 pipeline，并完成 VSwitch 创建 |
| `ask-waiting-resume` | ask user question 等待时杀进程，重启后重放问题并继续 |
| `image-initial` | 首轮用户输入通过 bracketed paste 粘贴静态 `initial.png` 图片，随后选择候选并完成 VSwitch 创建 |
| `image-ask-waiting-resume` | ask user question 等待时杀进程，`--continue` 恢复后通过静态图片回答澄清问题并继续 |
| `image-selection-waiting-resume` | 首轮图片启动 pipeline，candidate selection 等待时杀进程，重启后恢复选择 UI 并继续 |
| `image-normal-handoff` | pipeline handoff 到 normal chat 后，通过静态图片追问“你刚才创建了什么” |
| `image-interrupt` | evaluate candidates 阶段发送 Esc 后，通过静态图片输入回退到安全组的 interrupt 指令 |
| `selection-waiting-resume` | candidate selection 等待时杀进程，重启后恢复选择 UI 并继续 |
| `selection-invalid-then-valid` | candidate selection 中先发送无效选择，再发送有效选择并完成 |
| `evaluate-resume` | evaluate candidates 阶段杀进程，重启后重放中断点，发送 `continue` 后继续到选择并完成 |
| `rollback-step2` | architecture planning 中发送 Esc 和回退指令，验证 streaming interrupt 路径 |
| `rollback-step3` | pipeline 中发送 Esc 和回退指令，验证 REPL hard interrupt 路径 |
| `rollback-step4-selection` | candidate selection 中发送 Esc 和回退指令，验证 selection tabs interrupt 路径 |
| `rollback-step5-cleanup` | deploying 创建真实 ROS stack 后回退，验证旧 stack 被 cleanup 删除、新 stack 保留 |
| `rollback-step5-cleanup-recovery` | cleanup 删除中杀进程，`--continue` 恢复后验证 cleanup 重新触发并完成 |

## 验收标准

脚本的通过条件不是“进程退出 0”或“某个 regex 被等到”本身，而是 `summary.json` 里的所有
`checks` 都为 `true`。其中 `acceptance:` 前缀的检查项来自 PTY transcript，是回归验收标准：

- 通用：必须捕获到 PTY transcript，且 transcript 中不能出现 traceback、pexpect EOF/TIMEOUT、权限拒绝等终端错误。
- `scenario1`：必须展示 candidate selection，完成 pipeline，并在 PTY transcript 中出现 VSwitch 证据（例如 `VSwitchId`、`vsw-...` 或交换机 ID）；进入 normal chat 后，`你刚才创建了什么` 的回答必须提到 VSwitch/交换机，不能只验证“有输出”。
- `ask-waiting`：必须展示真实 `Ask user question`，回答澄清问题后如果进入 candidate selection，必须继续选择候选并完成 pipeline；最终 PTY transcript 必须出现 VSwitch 证据。
- `ask-waiting-resume`：必须在 `--continue` 后重放 `Ask user question`；回答后如果进入 candidate selection，必须继续选择候选并完成 pipeline；最终 PTY transcript 必须出现 VSwitch 证据。
- `image-initial`：必须记录 `initial` 静态图片 fixture 的 paste 事件；图片输入必须启动 pipeline、展示 candidate selection、完成 pipeline，并出现 VSwitch 证据。
- `image-ask-waiting-resume`：必须在 `--continue` 后重放 `Ask user question`；恢复后的回答必须通过 `ask-first-answer` 静态图片 fixture 输入；如果模型继续追问，runner 会再用 `ask-second-answer` 静态图片回答；最终必须继续到 candidate selection 或 pipeline completed，并出现 VSwitch 证据。
- `image-selection-waiting-resume`：必须记录 `initial` 静态图片 fixture 的 paste 事件；candidate selection 必须在 `--continue` 后重放；随后通过真实候选 UI 数字键选择并完成 pipeline，最终出现 VSwitch 证据。
- `image-normal-handoff`：必须完成 pipeline handoff normal chat；normal follow-up 必须通过 `normal-followup` 静态图片 fixture 输入，且回答必须提到 VSwitch/交换机。
- `image-interrupt`：必须先到达 `Evaluate candidates (3/5)`；发送 Esc 进入 interrupt 输入后，必须通过 `rollback-interrupt` 静态图片 fixture 输入；图片 interrupt 之后必须看到新的 pipeline 进展，回退后的输出必须指向安全组目标且不能指向 VSwitch。
- `selection-waiting-resume`：必须在 `--continue` 后重放 candidate selection，最终完成 pipeline，并出现 VSwitch 证据。
- `selection-invalid-then-valid`：必须记录无效选择输入，然后记录有效选择输入，最终完成 pipeline，并出现 VSwitch 证据。
- `evaluate-resume`：必须先到达 `Evaluate candidates (3/5)`，使用 `--continue` 恢复并重放该步骤；恢复后的普通 REPL prompt ready 后发送 `continue`，随后必须继续到 candidate selection 或 pipeline completed；最终 PTY transcript 必须出现 VSwitch 证据。
- `rollback-step2`：必须先到达 `Architecture planning (2/5)`；发送回退指令之后必须看到新的 pipeline 进展；回退后的输出必须指向安全组目标，且不能把用户输入 echo 里的“安全组”当作通过证据。
- `rollback-step3`：必须先到达 `Evaluate candidates (3/5)`；step3 的 parallel tabs 中断输入不要求 transcript 出现普通 `✎` prompt；发送回退指令之后必须看到新的 pipeline 进展（例如新的 `Intent parsing (1/5)`），不能用用户输入 echo 里的“回退”当作通过证据；回退后的输出必须指向安全组目标，且不能指向 VSwitch。
- `rollback-step4-selection`：必须先到达 `Confirm and select (4/5)`；selection tabs 中断输入同样不要求 transcript 出现普通 `✎` prompt；发送回退指令之后必须看到新的 pipeline 进展；回退后的输出必须指向安全组目标，且不能指向 VSwitch。
- `rollback-step5-cleanup`：必须到达 deploying 并观察到第一次 CreateStack；回退后 `cleanup.yaml` 必须把第一次 stack 记录为 cleanup target；第二次部署必须创建不同 stack；normal chat 前置 cleanup 必须完成；ROS GetStack 必须确认第一次 stack 已删除，第二次 stack 仍保留。
- `rollback-step5-cleanup-recovery`：在 `rollback-step5-cleanup` 的基础上，cleanup 开始后必须杀掉 REPL 子进程，随后用 `--continue` 恢复；恢复后必须重新触发 cleanup 并完成；ROS GetStack 同样必须确认旧 stack 删除、新 stack 保留。

会创建资源的非 cleanup 场景还必须在当前 REPL session 的 `pipeline/cleanup.yaml` 中观察到 ROS
`CreateStack` 资源，且 StackName 必须是 runner 为当前场景注入的 `iac-e2e-*` test-owned 名称；
teardown 前会通过 ROS GetStack 确认这些 stack 仍存在。场景结束后，runner 会自动删除这些
observed stack；删除前会再次校验云端 StackName 必须等于 ledger 记录的 test-owned StackName，
避免误删非本轮测试资源。

`rollback-step3` 和 `rollback-step4-selection` 会在发送 Esc 后等待第二个 raw-input ready 控制序列，再输入回退指令；这对应 tabs UI 切入中断文本输入行之后的真实 PTY 状态。

`rollback-step5-cleanup*` 的通过条件不只看 PTY 文本，还会读取同一个 REPL session 下的
`pipeline/cleanup.yaml`，并在最后写出 `acceptance-after-cleanup.ros-stack-states.json` 作为真实 ROS
状态快照。这样可以避免“终端看起来清理了，但 ledger 或云端状态不对”的假阳性。

pipeline completed 的匹配必须是终态证据，例如 `Pipeline completed`、`CREATE_COMPLETE`、`部署成功` 或
`Stack ID`；候选方案里的 `Completed` 或“参数选择完成”不能作为通过证据。

## 图片场景输入方式

REPL image 场景复用 `scripts/a2a/e2e/fixtures/text-images/` 下的静态 PNG fixture，避免每次运行时重新
生成图片。runner 不依赖系统剪贴板，而是通过 PTY 发送 bracketed paste 序列：

```text
ESC [ 200 ~ <absolute image path> ESC [ 201 ~
```

REPL 会把这个路径交给普通模式相同的 bracketed-paste 处理逻辑，解析图片文件、持久化到 image cache，
并在 prompt 中插入 `[Image #N]`。因此这些场景验证的是真实 REPL 图片入口，而不是测试脚本直接构造
`ImageBlock`。

## 产物

默认写入系统临时目录下的 `iac-code-repl-e2e-runs/<scenario>/<timestamp>-<pid>-<id>/`：

- `summary.json`：场景结果、检查点、耗时、失败原因。
- `events.jsonl`：spawn/send/expect/terminate 等黑盒终端事件。
- `child.env.json`：子进程环境摘要，敏感值会被脱敏。
- `transcript.raw.log`：脱敏后的原始终端 transcript。
- `transcript.normalized.log`：去 ANSI/control 字符后的 transcript，便于 diff 和排查。
- `acceptance-after-cleanup.ros-stack-states.json`：cleanup 场景的 ROS GetStack 快照，敏感值会被脱敏。

使用固定目录便于 CI 或本地脚本收集：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --scenario selection-waiting-resume \
  --run-dir "$(python - <<'PY'
import tempfile
from pathlib import Path
print(Path(tempfile.gettempdir()) / 'iac-code-repl-e2e-selection')
PY
)"
```

## 常用参数

- `--scenario` 可重复传入；默认只跑 `scenario1`。
- `--cwd` 指定 REPL 子进程工作目录；默认使用 run dir 下的 `workspace/`。
- `--timeout` 控制普通终端等待。
- `--stream-timeout` 控制 LLM/pipeline 长等待。
- `--selection-prompt` 指定候选方案选择输入；默认发送 `1` 选择第一个候选；传空字符串时直接回车确认。
- `--evaluate-resume-continue-prompt` 指定 `evaluate-resume` 在 `--continue` 重放后用于继续 running sidecar 的输入；默认 `continue`。
- `--cleanup-continue-prompt` 指定 `rollback-step5-cleanup-recovery` 在 `--continue` 恢复后用于继续 cleanup 的输入；默认只允许删除待清理列表中的 stack，避免误删其他资源。
- `--permission-prompt-response` 指定工具权限确认菜单的输入；默认 `pageup-enter`（发送 PageUp+Enter，选择第一项 `Yes, allow once`）。
- `--skip-final-teardown` 调试时跳过测试创建 stack 的最终删除；日常回归不要开启。
- `--leave-running` 调试时保留子进程，不自动 terminate。

## 与 pytest 的关系

`tests/repl_e2e/test_run_pipeline_scenarios.py` 只覆盖脚本的纯 helper、参数校验、脱敏、dispatch
流程，不会启动真实 REPL，也不会调用真实 LLM 或云账号。真实回归必须显式运行本目录脚本，并带上
`--allow-real-cloud`。
