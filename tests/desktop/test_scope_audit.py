from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest
import yaml


def _scope_module():
    from pathlib import Path

    script = Path(__file__).parents[2] / "desktop/scripts/scope_audit.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_scope_audit", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_working_tree_scope_audit_includes_all_git_path_sources(monkeypatch, tmp_path) -> None:
    module = _scope_module()
    allowlist = tmp_path / "scope-allowlist.txt"
    allowlist.write_text("desktop/**\ntests/desktop/**\n", encoding="utf-8")
    monkeypatch.setattr(module, "ALLOWLIST", allowlist)
    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: SimpleNamespace(base="origin/main", head="ignored", working_tree=True, enforce=False),
    )
    responses = {
        ("merge-base", "origin/main", "HEAD"): ["base-sha"],
        ("diff", "--name-only", "--diff-filter=ACMRD", "base-sha", "HEAD"): ["desktop/committed"],
        ("diff", "--name-only", "--diff-filter=ACMRD"): ["desktop/unstaged", "desktop/committed"],
        ("diff", "--cached", "--name-only", "--diff-filter=ACMRD"): ["desktop/staged"],
        ("ls-files", "--others", "--exclude-standard"): ["tests/desktop/test_new.py"],
    }
    observed: list[tuple[str, ...]] = []

    def git_lines(*args: str) -> list[str]:
        observed.append(args)
        return responses[args]

    monkeypatch.setattr(module, "_git_lines", git_lines)

    assert module.main() == 0
    assert ("merge-base", "origin/main", "HEAD") in observed
    assert len(observed) == 5


def test_working_tree_scope_audit_rejects_untracked_path_outside_allowlist(monkeypatch, tmp_path) -> None:
    module = _scope_module()
    allowlist = tmp_path / "scope-allowlist.txt"
    allowlist.write_text("desktop/**\n", encoding="utf-8")
    monkeypatch.setattr(module, "ALLOWLIST", allowlist)
    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: SimpleNamespace(base="origin/main", head="HEAD", working_tree=True, enforce=False),
    )

    def git_lines(*args: str) -> list[str]:
        if args[0] == "merge-base":
            return ["base-sha"]
        if args[0] == "ls-files":
            return ["desktop/owned.py", "src/outside.py"]
        return []

    monkeypatch.setattr(module, "_git_lines", git_lines)

    with pytest.raises(SystemExit, match="rejected 1 path"):
        module.main()


def test_scope_audit_skips_non_desktop_change(monkeypatch, tmp_path) -> None:
    module = _scope_module()
    allowlist = tmp_path / "scope-allowlist.txt"
    allowlist.write_text("desktop/**\n", encoding="utf-8")
    monkeypatch.setattr(module, "ALLOWLIST", allowlist)
    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: SimpleNamespace(base="origin/main", head="HEAD", working_tree=False, enforce=False),
    )

    def git_lines(*args: str) -> list[str]:
        if args[0] == "merge-base":
            return ["base-sha"]
        return ["src/iac_code/agent/loop.py", "tests/agent/test_loop.py"]

    monkeypatch.setattr(module, "_git_lines", git_lines)

    assert module.main() == 0


def test_scope_audit_enforce_rejects_shared_desktop_change_with_outside_path(monkeypatch, tmp_path) -> None:
    module = _scope_module()
    allowlist = tmp_path / "scope-allowlist.txt"
    allowlist.write_text("src/iac_code/web/app.py\n", encoding="utf-8")
    monkeypatch.setattr(module, "ALLOWLIST", allowlist)
    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: SimpleNamespace(base="origin/main", head="HEAD", working_tree=False, enforce=True),
    )

    def git_lines(*args: str) -> list[str]:
        if args[0] == "merge-base":
            return ["base-sha"]
        return ["src/iac_code/web/app.py", "src/iac_code/agent/loop.py"]

    monkeypatch.setattr(module, "_git_lines", git_lines)

    with pytest.raises(SystemExit, match="rejected 1 path"):
        module.main()


def test_desktop_workflow_forces_scope_audit_for_desktop_labeled_pr() -> None:
    from pathlib import Path

    workflow = (Path(__file__).parents[2] / ".github/workflows/desktop.yml").read_text(encoding="utf-8")
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)

    assert parsed["on"]["pull_request"]["types"] == ["opened", "synchronize", "reopened", "labeled"]
    assert "contains(github.event.pull_request.labels.*.name, 'desktop')" in workflow
    assert "scope_args+=(--enforce)" in workflow


def test_scope_allowlist_covers_desktop_branding_and_terminal_integration() -> None:
    from pathlib import Path

    allowlist = set(
        line.strip()
        for line in (Path(__file__).parents[2] / "desktop/scope-allowlist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert {
        "src/iac_code/commands/clear.py",
        "src/iac_code/ui/assets/iac-code-terminal-logo.png",
        "src/iac_code/ui/banner.py",
        "src/iac_code/ui/repl.py",
        "src/iac_code/ui/terminal_image.py",
        "src/iac_code/web/static/icons/iac-code-logo.svg",
        "tests/commands/test_clear.py",
        "tests/ui/test_banner.py",
        "tests/ui/test_repl_integration.py",
        "tests/ui/test_terminal_image.py",
        "website/docusaurus.config.ts",
        "website/src/clientModules/docsNavigation.test.cjs",
        "website/src/pages/index.module.css",
        "website/static/img/iac-code-logo.svg",
    } <= allowlist
