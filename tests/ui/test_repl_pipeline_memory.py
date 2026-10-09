from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from iac_code.memory.project_memory import ProjectMemoryRuntime
from iac_code.ui.repl import InlineREPL

REPL_SOURCE = Path("src/iac_code/ui/repl.py")


def test_repl_pipeline_creation_does_not_pass_full_memory_prompt_content() -> None:
    source = REPL_SOURCE.read_text(encoding="utf-8")

    assert "get_prompt_content()" not in source
    assert "memory_content_getter=(lambda: self._memory_manager.get_prompt_content()" not in source
    assert 'lambda: self._memory_manager.get_prompt_content() if self._memory_manager else ""' not in source


def test_repl_pipeline_creation_uses_explicit_pipeline_memory_policy_helper() -> None:
    source = REPL_SOURCE.read_text(encoding="utf-8")

    assert "def _pipeline_memory_content_getter(" in source
    assert source.count("memory_content_getter=self._pipeline_memory_content_getter(),") == 3


def test_pipeline_instructions_are_refreshed_without_auto_memory_bodies(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    monkeypatch.setenv("IAC_CODE_INSTRUCTION_MEMORY_FILE", "E2E-INSTRUCTIONS.md")
    instruction = config / "E2E-INSTRUCTIONS.md"
    instruction.write_text("StackName 必须以 iac-e2e-fixture- 开头。", encoding="utf-8")
    runtime = ProjectMemoryRuntime(str(workspace))
    repl = object.__new__(InlineREPL)
    repl._memory_runtime = runtime
    getter = repl._pipeline_memory_content_getter()
    assert "iac-e2e-fixture-" in getter()
    instruction.write_text("更换目标后保留 iac-e2e-new-fixture。", encoding="utf-8")
    assert "iac-e2e-new-fixture" in getter()
    assert "iac-e2e-fixture-" not in getter()
    repl._refresh_memory_context = lambda: SimpleNamespace(
        instruction_memory_content="explicit instruction", memory_mechanics_content="private auto-memory index")
    assert getter() == "explicit instruction"
