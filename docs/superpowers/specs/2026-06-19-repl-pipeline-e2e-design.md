# REPL Pipeline E2E Design

## Summary

Add a real end-to-end regression runner for the selling pipeline through the
interactive REPL terminal. This complements the existing A2A recovery runner:
A2A exercises the public JSON-RPC/SSE entrypoint, while this runner exercises
the user-facing PTY entrypoint, including Rich live rendering, raw keyboard
input, candidate selection UI, interrupt handling, resume replay, and handoff
from pipeline mode to normal chat.

The runner lives under `scripts/`, not `tests/`, because it intentionally uses
real provider configuration, real model calls, and real pipeline tools. It is a
manual/regression script like `scripts/a2a/e2e/run_recovery_scenarios.py`, not a
default pytest target.

## Goals

- Regress pipeline behavior through the real interactive terminal path.
- Use the real user's configured `~/.iac-code` by default.
- Use real LLM/provider calls and, for cloud scenarios, real cloud credentials.
- Drive the CLI as a black box through a pseudo-terminal.
- Capture transcripts and structured run summaries for debugging.
- Start with a small scenario set, then grow toward the A2A e2e matrix where it
  makes sense for the REPL surface.

## Non-Goals

- Do not add pytest tests that call real providers or real cloud APIs.
- Do not fake the LLM, provider, pipeline tools, or cloud APIs in this runner.
- Do not replace the A2A e2e runner.
- Do not duplicate low-level unit or component coverage already present under
  `tests/ui`, `tests/commands`, `tests/pipeline`, and `tests/a2a`.
- Do not make ordinary `make test` depend on this script.

## Location

Create:

```text
scripts/repl/e2e/run_pipeline_scenarios.py
scripts/repl/e2e/README.zh-CN.md
```

Optionally create `scripts/repl/e2e/common.py` if the runner grows beyond one
file. Keep the first implementation in one script unless shared helpers become
meaningful.

Update `scripts/README.md` to list the new REPL pipeline e2e runner.

## Execution Model

The runner starts `iac-code` in pipeline mode inside a PTY:

```bash
IAC_CODE_MODE=pipeline uv run iac-code --permission-mode bypass_permissions
```

The process is driven like a user:

- send text prompts with Enter;
- wait for visible terminal markers;
- send arrow keys or Enter for candidate selection;
- send Esc and follow-up text for hard interrupts;
- terminate or kill the process for recovery scenarios;
- restart with `--resume` or `--continue` when a scenario needs resume.

Use `pexpect` for the PTY harness. If it is not already available in the
project environment, add it via the project's dependency management in
`pyproject.toml` and `uv.lock`. A real PTY library is worth the small dependency
because this runner must send keys, wait on terminal text, handle timeouts, and
produce useful transcripts.

## Real Configuration

By default, the runner uses the current user's real environment and
configuration:

- do not override `HOME`;
- do not set `IAC_CODE_CONFIG_DIR`;
- do not create an isolated fake `.iac-code`;
- allow existing `~/.iac-code/settings.yml`, credentials, model, and provider
  state to be used.

The runner may accept optional one-run overrides:

```text
--provider dashscope
--model qwen3.6-plus
--api-base https://...
```

These become environment variables for the child process only. They must not
write `settings.yml`.

If the parent shell already has `IAC_CODE_CONFIG_DIR` set, the runner should
print it in the run summary so it is obvious that the real default
`~/.iac-code` was not used. It should not silently mutate it.

## Safety Gate

Require an explicit opt-in for scenarios that can call real providers and cloud
tools:

```text
--allow-real-cloud
```

This mirrors the A2A e2e runner. The name is intentionally the same because the
pipeline scenarios may deploy or clean real cloud resources.

Read-only or deterministic future scenarios may relax this gate, but the first
pipeline scenarios should require it.

## Run Artifacts

Unless `--run-root` or `--run-dir` is provided, write artifacts under:

```text
/tmp/iac-code-repl-e2e-runs/<scenario>/<timestamp>-<pid>-<suffix>/
```

Key files:

- `summary.json`: scenario result, checks, timings, command, cwd, session id if
  detected, and notes.
- `transcript.raw.log`: exact PTY output after redaction.
- `transcript.normalized.log`: ANSI-stripped and whitespace-normalized output
  used for checks.
- `events.jsonl`: runner actions and observations such as `spawn`, `send`,
  `expect`, `timeout`, `kill`, `restart`, and `check`.
- `child.env.json`: redacted child environment subset relevant to provider,
  mode, config dir, and model.

All logs must redact API keys, bearer tokens, secrets, passwords, and credential
values. Reuse the A2A redaction approach where practical.

## Initial Scenarios

### `scenario1`

Purpose: baseline full selling pipeline through the REPL.

Flow:

1. Start `iac-code` with `IAC_CODE_MODE=pipeline`.
2. Send `选择一个已有vpc，创建一个vswitch`.
3. Wait for candidate selection UI or equivalent candidate selection marker.
4. Select a candidate using the terminal interaction path.
5. Wait for pipeline completion and normal-chat handoff.
6. Send a normal follow-up prompt such as `你刚才创建了什么`.
7. Verify the terminal produces a non-empty answer and the run does not crash.
8. Exit cleanly.

Checks:

- pipeline started;
- candidate selection became visible;
- candidate selection input was accepted through the terminal;
- pipeline completed;
- normal-chat handoff happened;
- follow-up answer produced text.

### `ask-waiting`

Purpose: cover interactive clarification through the REPL.

Flow:

1. Start pipeline mode.
2. Send `我有个产品要上线`.
3. Wait for a clarification question.
4. Send a clarifying answer such as
   `我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。`
5. Continue until candidate selection or completion.

Checks:

- ask-user-question UI became visible;
- typed clarification was accepted;
- pipeline continued beyond the ask step.

### `selection-waiting-resume`

Purpose: cover persisted candidate selection and startup replay through the
interactive REPL.

Flow:

1. Start pipeline mode.
2. Send the baseline VSwitch prompt.
3. Wait until candidate selection is visible and waiting.
4. Kill the process.
5. Restart with `--continue` or `--resume <session>`.
6. Verify the candidate selection display replays.
7. Select a candidate.
8. Wait for pipeline completion.

Checks:

- waiting candidate selection was persisted;
- restart restored the same session;
- terminal candidate selection UI was usable after restore;
- pipeline completed after restored selection.

### `rollback-step3`

Purpose: cover REPL hard-interrupt rollback through Esc input.

Flow:

1. Start pipeline mode.
2. Send the baseline VSwitch prompt.
3. Wait until the pipeline reaches candidate generation or candidate evaluation.
4. Send Esc.
5. Type a rollback instruction, for example
   `回退到 intent_parsing，选择一个已有vpc，创建一个安全组`.
6. Wait for rollback feedback and subsequent progress.

Checks:

- Esc interrupt was observed by the REPL path;
- rollback instruction was accepted;
- pipeline reported rollback/progress after the interrupt.

This scenario can be timing-sensitive. If it proves flaky, keep it out of the
default scenario list and document it as a targeted regression.

## Future Scenario Parity

After the initial runner is stable, add REPL counterparts for high-value A2A
scenarios:

- `cancel-step1` through `cancel-step5`;
- `rollback-step1` through `rollback-step5`;
- `rollback-step5-cleanup`;
- `rollback-step5-cleanup-recovery`.

Do not force one-to-one parity where the A2A scenario validates protocol-only
behavior. REPL scenarios should focus on terminal-specific pipeline behavior.

## CLI

Proposed options:

```text
--scenario <name>       repeatable, defaults to scenario1
--allow-real-cloud      required for real pipeline scenarios
--cwd <path>            workspace for the child process, defaults to run dir workspace
--run-root <path>       artifact root
--run-dir <path>        exact artifact dir, only with one scenario
--python <command>      defaults to "uv run python"
--provider <name>       child env override only
--model <name>          child env override only
--api-base <url>        child env override only
--timeout <seconds>     default command/expect timeout
--stream-timeout <sec>  long wait for provider/pipeline progress
--leave-running         keep child process alive after failure for debugging
```

The spawned command should prefer module execution for consistency with A2A:

```text
<python command> -m iac_code.cli.main --permission-mode bypass_permissions
```

Set `IAC_CODE_MODE=pipeline` in the child environment.

## Checking Strategy

Terminal output is not a stable API. Checks should avoid brittle full-screen
snapshots. Prefer coarse markers:

- known step names or translated pipeline labels;
- candidate selection prompt/options;
- completion or handoff markers;
- non-empty assistant response after handoff;
- persisted session id or resume command if available.

Each expect operation should append an event to `events.jsonl` with the pattern,
timeout, pass/fail, and a short transcript tail. This makes failures diagnosable
without reading the whole raw log.

## Failure Handling

On failure:

1. append failure details to `events.jsonl`;
2. write `summary.json` with `passed=false`;
3. terminate the child process unless `--leave-running` is set;
4. print the run dir and failed checks.

On timeout, include the last transcript tail in both the terminal summary and
`summary.json`, after redaction.

## Documentation

`scripts/repl/e2e/README.zh-CN.md` should explain:

- this runner uses real `~/.iac-code`;
- it can call real providers and real cloud APIs;
- required safety flag;
- recommended first command;
- scenario descriptions;
- artifact locations;
- how to inspect transcripts after failure.

## Test Coverage For The Runner

Do not run real REPL scenarios in pytest. If we add tests for the runner itself,
place them under `tests/` and only test pure helpers such as argument parsing,
redaction, run directory creation, transcript normalization, and summary
formatting.
