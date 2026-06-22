from __future__ import annotations

from pathlib import Path

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
