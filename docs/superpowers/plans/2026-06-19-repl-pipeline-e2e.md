# REPL Pipeline E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a real PTY-driven `scripts/repl/e2e/run_pipeline_scenarios.py` runner that regresses selling pipeline behavior through the interactive REPL.

**Architecture:** The runner is a script, not a pytest suite. It uses `pexpect` to spawn `iac-code` in pipeline mode, writes redacted transcripts and JSON summaries under `/tmp`, and exposes scenario handlers for baseline, ask, resume, and interrupt flows. Pytest coverage only targets pure script helpers and scenario dispatch logic, never real LLM or cloud calls.

**Tech Stack:** Python 3.10+, `pexpect`, `argparse`, `json`, `tempfile`, `pathlib`, `pytest`, existing `uv` dependency management.

---

## File Structure

- Modify `pyproject.toml`: add `pexpect` to the `dev` dependency group.
- Modify `uv.lock`: refresh lockfile after adding `pexpect`.
- Create `scripts/repl/e2e/run_pipeline_scenarios.py`: CLI, PTY harness, redaction, transcript normalization, run artifact writing, scenario handlers.
- Create `scripts/repl/e2e/README.zh-CN.md`: usage, safety warning, scenario descriptions, artifact inspection.
- Modify `scripts/README.md`: list the new REPL pipeline e2e runner.
- Create `tests/repl_e2e/test_run_pipeline_scenarios.py`: helper and dispatch tests that do not spawn the real REPL.

## Task 1: Add Dependency And Helper Test Skeleton

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/repl_e2e/test_run_pipeline_scenarios.py`

- [ ] **Step 1: Write the failing import/helper tests**

Create `tests/repl_e2e/test_run_pipeline_scenarios.py` with this initial content:

```python
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "repl" / "e2e" / "run_pipeline_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_pipeline_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_args_defaults_to_scenario1() -> None:
    runner = _load_runner()

    args = runner.parse_args([])

    assert args.scenario is None
    assert runner._selected_scenarios(args) == ["scenario1"]
    assert args.python == "uv run python"


def test_validate_requires_real_cloud_flag() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "scenario1"])

    try:
        runner._validate_scenario_execution(args, "scenario1")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("scenario1 should require --allow-real-cloud")


def test_redaction_hides_sensitive_env_values() -> None:
    runner = _load_runner()

    text = "Authorization: Bearer sk-live-secret and token abcdefghijklmnop"
    env = {
        "IAC_CODE_API_KEY": "sk-live-secret",
        "CUSTOM_TOKEN": "abcdefghijklmnop",
        "IAC_CODE_MODEL": "qwen3.6-plus",
    }

    redacted = runner._redact_sensitive_text(text, env)

    assert "sk-live-secret" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "qwen3.6-plus" not in redacted
    assert "<redacted>" in redacted


def test_normalize_transcript_strips_ansi_and_control_noise() -> None:
    runner = _load_runner()

    normalized = runner._normalize_transcript("\x1b[31mPipeline\x1b[0m\r\n❯  hello\x08\x08ok")

    assert "\x1b" not in normalized
    assert "Pipeline" in normalized
    assert "ok" in normalized


def test_build_child_env_sets_pipeline_mode_without_overriding_home(monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.setenv("HOME", "/Users/example")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "/custom/iac")
    args = runner.parse_args(["--allow-real-cloud", "--provider", "dashscope", "--model", "qwen3.6-plus"])

    env = runner._build_child_env(args)

    assert env["HOME"] == "/Users/example"
    assert env["IAC_CODE_CONFIG_DIR"] == "/custom/iac"
    assert env["IAC_CODE_MODE"] == "pipeline"
    assert env["IAC_CODE_PROVIDER"] == "dashscope"
    assert env["IAC_CODE_MODEL"] == "qwen3.6-plus"
    assert env["PYTHONUTF8"] == "1"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: FAIL because `scripts/repl/e2e/run_pipeline_scenarios.py` does not exist.

- [ ] **Step 3: Add `pexpect` to project dependencies**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv add --dev pexpect
```

Expected: `pyproject.toml` gains `pexpect>=...` in `[dependency-groups].dev`, and `uv.lock` gains `pexpect` and `ptyprocess`.

- [ ] **Step 4: Verify dependency import**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python -c "import pexpect; print(pexpect.__version__)"
```

Expected: prints a pexpect version.

- [ ] **Step 5: Commit dependency and failing helper tests**

Run:

```bash
git add pyproject.toml uv.lock tests/repl_e2e/test_run_pipeline_scenarios.py
PATH="$HOME/.local/bin:$PATH" git commit -m "test: add REPL pipeline e2e helper tests"
```

Expected: commit succeeds after hooks pass or only expected failing tests remain uncommitted if hooks run the full suite. If hooks require passing tests, postpone this commit until Task 2.

## Task 2: Implement Runner Core Helpers

**Files:**
- Create: `scripts/repl/e2e/run_pipeline_scenarios.py`
- Modify: `tests/repl_e2e/test_run_pipeline_scenarios.py`

- [ ] **Step 1: Create the runner module with CLI and pure helpers**

Create `scripts/repl/e2e/run_pipeline_scenarios.py` with:

```python
#!/usr/bin/env python3
"""Run real interactive REPL pipeline E2E scenarios.

This runner intentionally drives the public terminal interface through a PTY.
It uses the user's real configuration by default and must not be imported by
ordinary package code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import pexpect
except ImportError:  # pragma: no cover - exercised manually when dependency missing
    pexpect = None  # type: ignore[assignment]


RUN_LOG_ROOT_NAME = "iac-code-repl-e2e-runs"
DEFAULT_INITIAL_PROMPT = "选择一个已有vpc，创建一个vswitch"
DEFAULT_SELECTION_PROMPT = ""
DEFAULT_ASK_PROMPT = "我有个产品要上线"
DEFAULT_ASK_ANSWER = "我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。"
DEFAULT_NORMAL_FOLLOWUP_PROMPT = "你刚才创建了什么"
DEFAULT_ROLLBACK_PROMPT = "回退到 intent_parsing，选择一个已有vpc，创建一个安全组"

PIPELINE_STARTED_PATTERNS = (r"Pipeline", r"pipeline", r"intent_parsing", r"意图")
CANDIDATE_SELECTION_PATTERNS = (r"confirm_and_select", r"候选", r"方案", r"选择")
ASK_PATTERNS = (r"请.*补充", r"需要.*信息", r"澄清", r"问题")
PIPELINE_COMPLETED_PATTERNS = (r"Pipeline completed", r"pipeline completed", r"完成", r"handoff", r"交接")
ROLLBACK_PATTERNS = (r"rollback", r"回退", r"重启", r"intent_parsing")


@dataclass
class ScenarioRunResult:
    scenario: str
    run_dir: str
    passed: bool
    checks: dict[str, bool]
    elapsed_seconds: float
    abort_reason: str = ""
    notes: list[str] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run interactive REPL pipeline E2E scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_SCENARIOS),
        help="Scenario to run. Can be repeated. Defaults to scenario1.",
    )
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--cwd", default="", help="Child process cwd. Defaults to <run-dir>/workspace.")
    parser.add_argument("--run-root", default=str(Path(tempfile.gettempdir()) / RUN_LOG_ROOT_NAME))
    parser.add_argument("--run-dir", default="", help="Explicit run dir. Only valid with one scenario.")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--terminal-width", type=int, default=140)
    parser.add_argument("--terminal-height", type=int, default=40)
    parser.add_argument("--leave-running", action="store_true")
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--selection-prompt", default=DEFAULT_SELECTION_PROMPT)
    parser.add_argument("--ask-prompt", default=DEFAULT_ASK_PROMPT)
    parser.add_argument("--ask-answer", default=DEFAULT_ASK_ANSWER)
    parser.add_argument("--normal-followup-prompt", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    parser.add_argument("--rollback-prompt", default=DEFAULT_ROLLBACK_PROMPT)
    return parser.parse_args(argv)


def _selected_scenarios(args: argparse.Namespace) -> list[str]:
    return args.scenario or ["scenario1"]


def _validate_scenario_execution(args: argparse.Namespace, scenario: str) -> None:
    if scenario in _REAL_CLOUD_SCENARIOS and not args.allow_real_cloud:
        raise SystemExit("refusing to run real REPL pipeline scenario without --allow-real-cloud: " + scenario)


def _split_python_command(value: str) -> list[str]:
    parts = shlex.split(value)
    if not parts:
        raise ValueError("--python must not be empty")
    return parts


def _build_child_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["IAC_CODE_MODE"] = "pipeline"
    if args.provider:
        env["IAC_CODE_PROVIDER"] = args.provider
    if args.model:
        env["IAC_CODE_MODEL"] = args.model
    if args.api_base:
        env["IAC_CODE_BASE_URL"] = args.api_base
    return env


def _redact_sensitive_text(text: str, env: dict[str, str] | None) -> str:
    redacted = text
    for name, value in (env or {}).items():
        if not value or len(value) < 6:
            continue
        upper = name.upper()
        if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            redacted = redacted.replace(value, "<redacted>")
    redacted = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,'\"}]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\s,'\"}]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-<redacted>", redacted)
    return redacted


_ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _normalize_transcript(text: str) -> str:
    text = _ANSI_PATTERN.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\b", "")
    return "\n".join(line.rstrip() for line in text.splitlines())


def _compact_text(text: str, *, max_chars: int = 800) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _new_run_dir(root: Path) -> Path:
    run_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return root / run_name


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def _redacted_env_summary(env: dict[str, str]) -> dict[str, str]:
    keys = ["HOME", "IAC_CODE_CONFIG_DIR", "IAC_CODE_MODE", "IAC_CODE_PROVIDER", "IAC_CODE_MODEL", "IAC_CODE_BASE_URL"]
    return {key: _redact_sensitive_text(env[key], env) for key in keys if key in env}


def _scenario_run_dir(args: argparse.Namespace, scenario: str) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    return _new_run_dir(Path(args.run_root).expanduser().resolve() / scenario)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scenarios = _selected_scenarios(args)
    if args.run_dir and len(scenarios) != 1:
        raise SystemExit("--run-dir can only be used with a single --scenario")
    for scenario in scenarios:
        _validate_scenario_execution(args, scenario)
    results = [_SCENARIOS[scenario](args, scenario) for scenario in scenarios]
    return 0 if all(code == 0 for code in results) else 1


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    raise NotImplementedError


def run_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    raise NotImplementedError


def run_selection_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    raise NotImplementedError


def run_rollback_step3(args: argparse.Namespace, scenario: str) -> int:
    raise NotImplementedError


_SCENARIOS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "scenario1": run_scenario1,
    "ask-waiting": run_ask_waiting,
    "selection-waiting-resume": run_selection_waiting_resume,
    "rollback-step3": run_rollback_step3,
}
_REAL_CLOUD_SCENARIOS = frozenset(_SCENARIOS)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run helper tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: PASS for helper tests.

- [ ] **Step 3: Add parser test for repeated scenarios and run-dir validation**

Append to `tests/repl_e2e/test_run_pipeline_scenarios.py`:

```python
def test_repeated_scenarios_are_preserved() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--scenario", "scenario1", "--scenario", "ask-waiting", "--allow-real-cloud"])

    assert runner._selected_scenarios(args) == ["scenario1", "ask-waiting"]


def test_run_dir_requires_single_scenario() -> None:
    runner = _load_runner()

    try:
        runner.main([
            "--scenario",
            "scenario1",
            "--scenario",
            "ask-waiting",
            "--allow-real-cloud",
            "--run-dir",
            "/tmp/repl-e2e",
        ])
    except SystemExit as exc:
        assert "--run-dir can only be used with a single --scenario" in str(exc)
    else:
        raise AssertionError("--run-dir should reject multiple scenarios")
```

- [ ] **Step 4: Run helper tests again**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit core helper implementation**

Run:

```bash
git add scripts/repl/e2e/run_pipeline_scenarios.py tests/repl_e2e/test_run_pipeline_scenarios.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add REPL pipeline e2e runner core"
```

Expected: commit succeeds.

## Task 3: Implement PTY Harness And Artifact Writing

**Files:**
- Modify: `scripts/repl/e2e/run_pipeline_scenarios.py`
- Modify: `tests/repl_e2e/test_run_pipeline_scenarios.py`

- [ ] **Step 1: Add unit tests for artifact result serialization**

Append to `tests/repl_e2e/test_run_pipeline_scenarios.py`:

```python
def test_write_result_writes_summary_and_transcripts(tmp_path: Path) -> None:
    runner = _load_runner()
    result = runner.ScenarioRunResult(
        scenario="scenario1",
        run_dir=str(tmp_path),
        passed=True,
        checks={"pipeline started": True},
        elapsed_seconds=1.25,
    )

    runner._write_run_artifacts(
        run_dir=tmp_path,
        env={"IAC_CODE_API_KEY": "sk-secret123456", "IAC_CODE_MODEL": "qwen3.6-plus"},
        raw_transcript="hello sk-secret123456",
        events=[{"type": "check", "name": "pipeline started", "passed": True}],
        result=result,
    )

    summary = (tmp_path / "summary.json").read_text(encoding="utf-8")
    raw = (tmp_path / "transcript.raw.log").read_text(encoding="utf-8")
    normalized = (tmp_path / "transcript.normalized.log").read_text(encoding="utf-8")
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")

    assert "sk-secret123456" not in summary
    assert "sk-secret123456" not in raw
    assert "sk-secret123456" not in normalized
    assert "pipeline started" in events
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py::test_write_result_writes_summary_and_transcripts -v
```

Expected: FAIL because `_write_run_artifacts` is not implemented.

- [ ] **Step 3: Implement `_write_run_artifacts` and `ReplPty`**

Add to `scripts/repl/e2e/run_pipeline_scenarios.py` above the scenario functions:

```python
class ReplPty:
    def __init__(self, *, args: argparse.Namespace, run_dir: Path, cwd: Path, env: dict[str, str]) -> None:
        if pexpect is None:
            raise RuntimeError("pexpect is required. Install dependencies with: uv sync --all-extras")
        self.args = args
        self.run_dir = run_dir
        self.cwd = cwd
        self.env = env
        self.events: list[dict[str, Any]] = []
        self.raw_chunks: list[str] = []
        self.child: Any | None = None

    @property
    def transcript(self) -> str:
        return "".join(self.raw_chunks)

    def spawn(self, *, extra_args: list[str] | None = None) -> None:
        command = [
            *_split_python_command(self.args.python),
            "-m",
            "iac_code.cli.main",
            "--permission-mode",
            "bypass_permissions",
            *(extra_args or []),
        ]
        self.events.append({"type": "spawn", "command": command, "cwd": str(self.cwd), "at": _utc_now()})
        self.child = pexpect.spawn(
            command[0],
            command[1:],
            cwd=str(self.cwd),
            env=self.env,
            encoding="utf-8",
            codec_errors="replace",
            timeout=self.args.timeout,
            dimensions=(self.args.terminal_height, self.args.terminal_width),
        )

    def sendline(self, text: str) -> None:
        self._require_child().sendline(text)
        self.events.append({"type": "sendline", "text": _redact_sensitive_text(text, self.env), "at": _utc_now()})

    def send(self, text: str, *, label: str = "send") -> None:
        self._require_child().send(text)
        self.events.append({"type": label, "text": _redact_sensitive_text(text, self.env), "at": _utc_now()})

    def expect_any(self, patterns: tuple[str, ...], *, description: str, timeout: float) -> str:
        child = self._require_child()
        try:
            index = child.expect(list(patterns), timeout=timeout)
            self._capture_child_output(child.before + child.after)
            matched = patterns[index]
            self.events.append(
                {"type": "expect", "description": description, "pattern": matched, "passed": True, "at": _utc_now()}
            )
            return matched
        except Exception as exc:
            self._capture_child_output(getattr(child, "before", "") or "")
            tail = _compact_text(_normalize_transcript(self.transcript)[-2000:])
            self.events.append(
                {
                    "type": "expect",
                    "description": description,
                    "patterns": list(patterns),
                    "passed": False,
                    "error": str(exc),
                    "tail": _redact_sensitive_text(tail, self.env),
                    "at": _utc_now(),
                }
            )
            raise

    def terminate(self, *, force: bool = False) -> None:
        child = self.child
        if child is None:
            return
        try:
            if force:
                child.kill(signal.SIGKILL)
            else:
                child.terminate(force=True)
        finally:
            self._capture_child_output(getattr(child, "before", "") or "")
            self.events.append({"type": "terminate", "force": force, "at": _utc_now()})

    def _capture_child_output(self, text: str) -> None:
        if text:
            self.raw_chunks.append(text)

    def _require_child(self) -> Any:
        if self.child is None:
            raise RuntimeError("REPL child has not been spawned")
        return self.child


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_run_artifacts(
    *,
    run_dir: Path,
    env: dict[str, str],
    raw_transcript: str,
    events: list[dict[str, Any]],
    result: ScenarioRunResult,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    redacted_raw = _redact_sensitive_text(raw_transcript, env)
    normalized = _normalize_transcript(redacted_raw)
    (run_dir / "transcript.raw.log").write_text(redacted_raw, encoding="utf-8")
    (run_dir / "transcript.normalized.log").write_text(normalized, encoding="utf-8")
    _write_json(run_dir / "child.env.json", _redacted_env_summary(env))
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(_redact_json_value(event, env), ensure_ascii=False, default=str) + "\n")
    _write_json(run_dir / "summary.json", _redact_json_value(asdict(result), env))


def _redact_json_value(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value, env)
    if isinstance(value, list):
        return [_redact_json_value(item, env) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            upper = key_text.upper()
            if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTHORIZATION")):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_json_value(item, env)
        return redacted
    return value
```

- [ ] **Step 4: Run targeted artifact test**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py::test_write_result_writes_summary_and_transcripts -v
```

Expected: PASS.

- [ ] **Step 5: Run all runner helper tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit PTY harness**

Run:

```bash
git add scripts/repl/e2e/run_pipeline_scenarios.py tests/repl_e2e/test_run_pipeline_scenarios.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add REPL PTY e2e harness"
```

Expected: commit succeeds.

## Task 4: Implement Pipeline Scenario Handlers

**Files:**
- Modify: `scripts/repl/e2e/run_pipeline_scenarios.py`
- Modify: `tests/repl_e2e/test_run_pipeline_scenarios.py`

- [ ] **Step 1: Add dispatch tests with fake harness**

Append:

```python
def test_scenario1_runs_expected_terminal_flow(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    actions: list[tuple[str, str]] = []

    class FakePty:
        def __init__(self, *, args, run_dir, cwd, env):
            self.events = []
            self.transcript = "Pipeline 候选 完成 normal answer"

        def spawn(self, *, extra_args=None):
            actions.append(("spawn", ""))

        def sendline(self, text):
            actions.append(("sendline", text))

        def expect_any(self, patterns, *, description, timeout):
            actions.append(("expect", description))
            return patterns[0]

        def send(self, text, *, label="send"):
            actions.append((label, text))

        def terminate(self, *, force=False):
            actions.append(("terminate", str(force)))

    monkeypatch.setattr(runner, "ReplPty", FakePty)
    args = runner.parse_args(["--allow-real-cloud", "--run-dir", str(tmp_path)])

    assert runner.run_scenario1(args, "scenario1") == 0
    assert ("sendline", runner.DEFAULT_INITIAL_PROMPT) in actions
    assert ("sendline", runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT) in actions
    assert ("sendline", "/exit") in actions
```

- [ ] **Step 2: Run dispatch test to verify failure**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py::test_scenario1_runs_expected_terminal_flow -v
```

Expected: FAIL because scenario handlers still raise `NotImplementedError`.

- [ ] **Step 3: Implement harness orchestration and scenario handlers**

Replace the `NotImplementedError` scenario functions in `run_pipeline_scenarios.py` with:

```python
def _run_with_pty(
    args: argparse.Namespace,
    scenario: str,
    callback: Callable[[ReplPty, dict[str, bool]], None],
) -> int:
    started = time.monotonic()
    run_dir = _scenario_run_dir(args, scenario)
    workspace_dir = Path(args.cwd).expanduser().resolve() if args.cwd else run_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    env = _build_child_env(args)
    pty = ReplPty(args=args, run_dir=run_dir, cwd=workspace_dir, env=env)
    checks: dict[str, bool] = {}
    notes: list[str] = []
    abort_reason = ""
    passed = False
    try:
        pty.spawn()
        callback(pty, checks)
        passed = all(checks.values()) if checks else True
    except Exception as exc:
        abort_reason = f"{type(exc).__name__}: {exc}"
        notes.append(abort_reason)
        passed = False
    finally:
        if not args.leave_running:
            pty.terminate()
        result = ScenarioRunResult(
            scenario=scenario,
            run_dir=str(run_dir),
            passed=passed,
            checks=checks,
            elapsed_seconds=round(time.monotonic() - started, 3),
            abort_reason=abort_reason,
            notes=notes,
        )
        _write_run_artifacts(run_dir=run_dir, env=env, raw_transcript=pty.transcript, events=pty.events, result=result)
        _print_result(result)
    return 0 if passed else 1


def _print_result(result: ScenarioRunResult) -> None:
    print(f"\nscenario: {result.scenario}")
    print(f"run_dir: {result.run_dir}")
    if result.notes:
        print("\nnotes:")
        for note in result.notes:
            print(f"  - {_compact_text(note, max_chars=1000)}")
    print("\nchecks:")
    for name, passed in result.checks.items():
        print(f"  {'OK' if passed else 'FAIL'} {name}")
    print(f"\nRESULT: {'PASS' if result.passed else 'FAIL'}")


def _select_default_candidate(pty: ReplPty, args: argparse.Namespace) -> None:
    if args.selection_prompt:
        pty.sendline(args.selection_prompt)
    else:
        pty.send("\r", label="select-default-candidate")


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        pty.expect_any((r"❯", r">", r"pipeline"), description="initial prompt", timeout=args.timeout)
        pty.sendline(args.initial_prompt)
        pty.expect_any(PIPELINE_STARTED_PATTERNS, description="pipeline started", timeout=args.stream_timeout)
        checks["pipeline started"] = True
        pty.expect_any(CANDIDATE_SELECTION_PATTERNS, description="candidate selection visible", timeout=args.stream_timeout)
        checks["candidate selection became visible"] = True
        _select_default_candidate(pty, args)
        checks["candidate selection input sent"] = True
        pty.expect_any(PIPELINE_COMPLETED_PATTERNS, description="pipeline completed", timeout=args.stream_timeout)
        checks["pipeline completed"] = True
        pty.sendline(args.normal_followup_prompt)
        pty.expect_any((r".{20,}",), description="normal follow-up produced output", timeout=args.stream_timeout)
        checks["normal follow-up produced text"] = True
        pty.sendline("/exit")
    return _run_with_pty(args, scenario, callback)


def run_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        pty.expect_any((r"❯", r">", r"pipeline"), description="initial prompt", timeout=args.timeout)
        pty.sendline(args.ask_prompt)
        pty.expect_any(ASK_PATTERNS, description="ask question visible", timeout=args.stream_timeout)
        checks["ask question became visible"] = True
        pty.sendline(args.ask_answer)
        checks["ask answer sent"] = True
        pty.expect_any(CANDIDATE_SELECTION_PATTERNS + PIPELINE_COMPLETED_PATTERNS, description="pipeline continued after ask", timeout=args.stream_timeout)
        checks["pipeline continued beyond ask"] = True
        pty.sendline("/exit")
    return _run_with_pty(args, scenario, callback)


def run_selection_waiting_resume(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        pty.expect_any((r"❯", r">", r"pipeline"), description="initial prompt", timeout=args.timeout)
        pty.sendline(args.initial_prompt)
        pty.expect_any(CANDIDATE_SELECTION_PATTERNS, description="candidate selection visible", timeout=args.stream_timeout)
        checks["candidate selection became visible before kill"] = True
        pty.terminate(force=True)
        checks["first process killed"] = True
        pty.spawn(extra_args=["--continue"])
        pty.expect_any(CANDIDATE_SELECTION_PATTERNS, description="candidate selection replayed", timeout=args.stream_timeout)
        checks["candidate selection replayed"] = True
        _select_default_candidate(pty, args)
        pty.expect_any(PIPELINE_COMPLETED_PATTERNS, description="pipeline completed after resume", timeout=args.stream_timeout)
        checks["pipeline completed after resume"] = True
        pty.sendline("/exit")
    return _run_with_pty(args, scenario, callback)


def run_rollback_step3(args: argparse.Namespace, scenario: str) -> int:
    def callback(pty: ReplPty, checks: dict[str, bool]) -> None:
        pty.expect_any((r"❯", r">", r"pipeline"), description="initial prompt", timeout=args.timeout)
        pty.sendline(args.initial_prompt)
        pty.expect_any((r"evaluate_candidates", r"候选", r"方案"), description="candidate evaluation visible", timeout=args.stream_timeout)
        checks["candidate evaluation reached"] = True
        pty.send("\x1b", label="send-esc")
        checks["esc sent"] = True
        pty.expect_any((r"✎", r"interrupt", r"输入", r"Judging"), description="interrupt input visible", timeout=args.timeout)
        pty.sendline(args.rollback_prompt)
        checks["rollback prompt sent"] = True
        pty.expect_any(ROLLBACK_PATTERNS, description="rollback progress visible", timeout=args.stream_timeout)
        checks["rollback progress visible"] = True
        pty.sendline("/exit")
    return _run_with_pty(args, scenario, callback)
```

- [ ] **Step 4: Run scenario dispatch tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: PASS.

- [ ] **Step 5: Run script help without real cloud**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

Expected: prints CLI help and exits 0.

- [ ] **Step 6: Commit scenario implementation**

Run:

```bash
git add scripts/repl/e2e/run_pipeline_scenarios.py tests/repl_e2e/test_run_pipeline_scenarios.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add REPL pipeline e2e scenarios"
```

Expected: commit succeeds.

## Task 5: Add Documentation

**Files:**
- Create: `scripts/repl/e2e/README.zh-CN.md`
- Modify: `scripts/README.md`

- [ ] **Step 1: Add README**

Create `scripts/repl/e2e/README.zh-CN.md`:

```markdown
# REPL Pipeline E2E

本目录包含通过真实交互式终端回归 pipeline 功能的脚本。它和
`scripts/a2a/e2e/run_recovery_scenarios.py` 目标相同，都是回归 pipeline；
区别是这里走真实 REPL / PTY 入口，而 A2A runner 走 JSON-RPC / SSE 入口。

## 重要说明

- 默认使用当前用户真实 `~/.iac-code` 配置。
- 会调用真实 LLM provider。
- 带 `--allow-real-cloud` 的 pipeline 场景可能调用真实阿里云工具和凭证。
- 不属于普通 `make test`，也不会在 pytest 中执行真实场景。

## 快速开始

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1
```

指定 provider/model 但不写入 `settings.yml`：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --provider dashscope \
  --model qwen3.6-plus \
  --scenario scenario1
```

## 场景

| 场景 | 覆盖 |
| --- | --- |
| `scenario1` | 通过 REPL 完成 VSwitch pipeline、候选方案选择、handoff normal chat |
| `ask-waiting` | 通过 REPL 回复澄清问题后继续 pipeline |
| `selection-waiting-resume` | candidate selection 等待时杀进程，重启后恢复选择 UI 并继续 |
| `rollback-step3` | pipeline 中发送 Esc 和回退指令，验证 REPL hard interrupt 路径 |

## 产物

默认写入：

```text
/tmp/iac-code-repl-e2e-runs/<scenario>/<timestamp>-<pid>-<suffix>/
```

关键文件：

- `summary.json`
- `events.jsonl`
- `transcript.raw.log`
- `transcript.normalized.log`
- `child.env.json`

失败时优先看 `summary.json` 和 `events.jsonl`，再看 normalized transcript。
```

- [ ] **Step 2: Update scripts README**

Modify `scripts/README.md` layout table to add:

```markdown
| `repl/e2e/` | Interactive REPL pipeline end-to-end scenario runner. |
```

Add command:

```markdown
uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

- [ ] **Step 3: Run docs-adjacent smoke commands**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: help exits 0 and tests pass.

- [ ] **Step 4: Commit docs**

Run:

```bash
git add scripts/repl/e2e/README.zh-CN.md scripts/README.md
PATH="$HOME/.local/bin:$PATH" git commit -m "docs: document REPL pipeline e2e runner"
```

Expected: commit succeeds.

## Task 6: Final Verification

**Files:**
- No new files unless verification exposes needed fixes.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -v
```

Expected: PASS.

- [ ] **Step 2: Run script help**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

Expected: exits 0 and lists all scenarios.

- [ ] **Step 3: Run lint for touched areas**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run ruff check scripts/repl/e2e/run_pipeline_scenarios.py tests/repl_e2e/test_run_pipeline_scenarios.py
PATH="$HOME/.local/bin:$PATH" uv run ruff format --check scripts/repl/e2e/run_pipeline_scenarios.py tests/repl_e2e/test_run_pipeline_scenarios.py
```

Expected: PASS.

- [ ] **Step 4: Do not run real scenario automatically**

Do not run this during automated verification:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python scripts/repl/e2e/run_pipeline_scenarios.py --allow-real-cloud --scenario scenario1
```

Reason: it intentionally uses real `~/.iac-code`, real LLM, and potentially real cloud resources. Leave it as the manual regression command in the final response.

- [ ] **Step 5: Final status**

Run:

```bash
git status --short
```

Expected: clean working tree.
