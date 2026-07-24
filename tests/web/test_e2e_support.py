import ast
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
E2E_ROOT = REPO_ROOT / "scripts" / "web" / "e2e"
FAKE_SERVER = E2E_ROOT / "fake_web_server.py"
RUNTIME_ENV = E2E_ROOT / "runtime_env.mjs"
SMOKE_SCRIPT = E2E_ROOT / "web_repl_smoke.mjs"
VISUAL_AUDIT_SCRIPT = E2E_ROOT / "web_repl_visual_audit.mjs"


def _fake_runner_method_keywords(method_name: str) -> set[str]:
    module = ast.parse(FAKE_SERVER.read_text(encoding="utf-8"))
    runner = next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "FakePipelineActionRunner"
    )
    method = next(
        node
        for node in runner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    )
    return {argument.arg for argument in method.args.kwonlyargs}


def _run_scrubbed_env(tmp_path: Path, *, source_env: dict[str, str], platform: str) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "runtime-env-check.mjs"
    script.write_text(
        textwrap.dedent(
            f"""
            import {{ scrubbedChildEnv }} from {json.dumps(RUNTIME_ENV.as_uri())};
            const result = scrubbedChildEnv({{
              configDir: "/tmp/config",
              homeDir: "/tmp/home",
              repoRoot: "/tmp/repo",
              sourceEnv: {json.dumps(source_env)},
              platform: {json.dumps(platform)},
            }});
            process.stdout.write(JSON.stringify(result));
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_fake_pipeline_action_runner_matches_interrupt_model_selection_contract() -> None:
    assert "model_selection" in _fake_runner_method_keywords("interrupt")


def test_smoke_script_exercises_pipeline_interrupt_route() -> None:
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "pipeline interrupt accepts the frozen model selection" in source
    assert "/interrupt`" in source


def test_scrubbed_child_env_preserves_case_insensitive_windows_runtime_keys(tmp_path: Path) -> None:
    env = _run_scrubbed_env(
        tmp_path,
        source_env={
            "Path": r"C:\\Tools",
            "SystemRoot": r"C:\\Windows",
            "ComSpec": r"C:\\Windows\\System32\\cmd.exe",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "USERPROFILE": r"C:\\Users\\tester",
            "APPDATA": r"C:\\Users\\tester\\AppData\\Roaming",
            "LOCALAPPDATA": r"C:\\Users\\tester\\AppData\\Local",
            "TEMP": r"C:\\Temp",
            "OPENAI_API_KEY": "must-not-leak",
        },
        platform="win32",
    )

    for name in ("Path", "SystemRoot", "ComSpec", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP"):
        assert env[name]
    assert env["USERPROFILE"] == "/tmp/home"
    assert env["APPDATA"] == "/tmp/home\\AppData\\Roaming"
    assert env["LOCALAPPDATA"] == "/tmp/home\\AppData\\Local"
    assert "SHELL" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["IAC_CODE_CONFIG_DIR"] == "/tmp/config"
    assert env["IAC_CODE_CWD"] == "/tmp/repo"


def test_scrubbed_child_env_sets_isolated_posix_home_and_shell(tmp_path: Path) -> None:
    env = _run_scrubbed_env(
        tmp_path,
        source_env={"PATH": "/usr/bin", "HOME": "/real/home", "SHELL": "/bin/bash", "ALIYUN_TOKEN": "secret"},
        platform="darwin",
    )

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/home"
    assert env["SHELL"] == "/bin/zsh"
    assert "ALIYUN_TOKEN" not in env


def test_visual_audit_removes_both_isolated_runtime_directories() -> None:
    source = VISUAL_AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "fs.rmSync(configDir, { recursive: true, force: true });" in source
    assert "fs.rmSync(homeDir, { recursive: true, force: true });" in source


def test_visual_audit_uses_objective_layout_gates_instead_of_synthetic_scores() -> None:
    source = VISUAL_AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "scoreForScreenshot" not in source
    assert 'structure: "3.8"' not in source
    assert 'hierarchy: "3.8"' not in source
    assert "missingKeyElements" in source
    assert "collapsedKeyElements" in source
    assert "occludedKeyElements" in source
    assert "overlayOpen\n      ? []" not in source


def test_visual_audit_fails_the_process_for_blocking_findings() -> None:
    source = VISUAL_AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "blockingFindings" in source
    assert "Visual audit failed with" in source
    assert "throw new Error" in source
