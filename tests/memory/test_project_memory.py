from __future__ import annotations

import importlib
import stat


def _module():
    return importlib.import_module("iac_code.memory.project_memory")


def test_project_memory_dir_uses_git_root_and_config_dir(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    git_dir = repo / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    memory_dir = mod.get_project_memory_dir(str(nested))

    assert memory_dir == config_dir / "projects" / mod.project_key_for_cwd(str(repo)) / "memory"


def test_project_memory_runtime_defaults_instruction_files_to_agents_md(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    runtime = mod.ProjectMemoryRuntime(str(project))

    assert runtime.user_instruction_path == config_dir / "AGENTS.md"
    assert runtime.project_instruction_path == project / "AGENTS.md"
    assert runtime.auto_memory_dir == config_dir / "projects" / mod.project_key_for_cwd(str(project)) / "memory"
    assert runtime.memory_manager._memory_dir == runtime.auto_memory_dir


def test_project_memory_dir_uses_logical_path_for_symlinked_workspace(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    physical_root = tmp_path / "mount-root"
    physical_root.mkdir()
    logical_root = tmp_path / "workspace"
    logical_root.symlink_to(physical_root, target_is_directory=True)
    logical_cwd = logical_root / "ctx-1"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    runtime = mod.ProjectMemoryRuntime(str(logical_cwd))

    expected_key = mod.sanitize_path(str(logical_cwd))
    assert runtime.auto_memory_dir == config_dir / "projects" / expected_key / "memory"
    assert runtime.memory_manager._memory_dir == runtime.auto_memory_dir


def test_project_memory_dir_uses_logical_git_root_for_symlinked_workspace(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    physical_root = tmp_path / "mount-root" / "oss" / "bucket"
    physical_root.mkdir(parents=True)
    (physical_root / ".git").mkdir()
    logical_root = tmp_path / "workspace"
    logical_root.symlink_to(physical_root, target_is_directory=True)
    logical_cwd = logical_root / "ctx-1"
    logical_cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    runtime = mod.ProjectMemoryRuntime(str(logical_cwd))

    expected_key = mod.sanitize_path(str(logical_root))
    assert runtime.project_root == logical_root
    assert runtime.auto_memory_dir == config_dir / "projects" / expected_key / "memory"


def test_project_memory_runtime_allows_instruction_file_env_override(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_INSTRUCTION_MEMORY_FILE", "IAC-CODE.md")

    runtime = mod.ProjectMemoryRuntime(str(project))

    assert runtime.user_instruction_path == config_dir / "IAC-CODE.md"
    assert runtime.project_instruction_path == project / "IAC-CODE.md"


def test_build_memory_context_reads_instruction_files_without_memory_index(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    runtime = mod.ProjectMemoryRuntime(str(project))
    runtime.user_instruction_path.parent.mkdir(parents=True, exist_ok=True)
    runtime.user_instruction_path.write_text("User instruction\n", encoding="utf-8")
    runtime.project_instruction_path.write_text("Project instruction\n", encoding="utf-8")
    runtime.memory_manager.save(
        "topic-a",
        content="Topic body should not be always injected",
        memory_type="project",
        description="Topic A",
    )

    context = runtime.build_memory_context()

    assert "User instruction" in context.instruction_memory_content
    assert "Project instruction" in context.instruction_memory_content
    assert context.memory_index_content == ""
    assert "topic-a.md" not in context.instruction_memory_content
    assert "Topic body should not be always injected" not in context.instruction_memory_content
    assert "read_memory" in context.memory_mechanics_content
    assert "write_memory" in context.memory_mechanics_content


def test_ensure_user_instruction_file_returns_path_without_creating_empty_file(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    runtime = mod.ProjectMemoryRuntime(str(project))

    created = runtime.ensure_instruction_file("user")

    assert created == config_dir / "AGENTS.md"
    assert not created.exists()


def test_ensure_project_instruction_file_returns_path_without_creating_empty_file(tmp_path, monkeypatch):
    mod = _module()
    config_dir = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    project.chmod(0o755)
    original_mode = stat.S_IMODE(project.stat().st_mode)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    runtime = mod.ProjectMemoryRuntime(str(project))

    created = runtime.ensure_instruction_file("project")

    assert created == project / "AGENTS.md"
    assert not created.exists()
    assert stat.S_IMODE(project.stat().st_mode) == original_mode


def test_auto_memory_enabled_defaults_to_true_and_persists(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    assert mod.is_auto_memory_enabled() is True

    mod.save_auto_memory_enabled(False)

    assert mod.is_auto_memory_enabled() is False
