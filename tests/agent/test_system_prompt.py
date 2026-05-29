import tempfile
from pathlib import Path
from unittest.mock import patch

from iac_code.agent.system_prompt import (
    DYNAMIC_BOUNDARY,
    SystemPromptBuilder,
    _build_cloud_config_section,
    _build_environment_section,
    build_system_prompt,
    split_by_dynamic_boundary,
)
from iac_code.utils.project_paths import _read_git_head

_TMP = tempfile.gettempdir()


class TestSystemPromptBuilder:
    def test_cached_section_computed_once(self):
        builder = SystemPromptBuilder()
        call_count = 0

        def compute():
            nonlocal call_count
            call_count += 1
            return "cached content"

        builder.add_cached_section("test", compute, priority=100)
        builder.build()
        builder.build()
        assert call_count == 1

    def test_uncached_section_computed_each_time(self):
        builder = SystemPromptBuilder()
        call_count = 0

        def compute():
            nonlocal call_count
            call_count += 1
            return f"dynamic {call_count}"

        builder.add_uncached_section("test", compute, priority=100)
        result1 = builder.build()
        result2 = builder.build()
        assert call_count == 2
        assert "dynamic 1" in result1
        assert "dynamic 2" in result2

    def test_invalidate_clears_cache(self):
        builder = SystemPromptBuilder()
        call_count = 0

        def compute():
            nonlocal call_count
            call_count += 1
            return f"v{call_count}"

        builder.add_cached_section("test", compute, priority=100)
        builder.build()
        assert call_count == 1
        builder.invalidate()
        builder.build()
        assert call_count == 2

    def test_priority_ordering(self):
        builder = SystemPromptBuilder()
        builder.add_cached_section("low", lambda: "LOW", priority=10)
        builder.add_cached_section("high", lambda: "HIGH", priority=100)
        result = builder.build()
        assert result.index("HIGH") < result.index("LOW")

    def test_dynamic_boundary_present(self):
        builder = SystemPromptBuilder()
        builder.add_cached_section("static", lambda: "STATIC", priority=100, is_static=True)
        builder.add_cached_section("dynamic", lambda: "DYNAMIC", priority=50, is_static=False)
        result = builder.build()
        assert DYNAMIC_BOUNDARY in result
        assert result.index("STATIC") < result.index(DYNAMIC_BOUNDARY)
        assert result.index(DYNAMIC_BOUNDARY) < result.index("DYNAMIC")


class TestBuildSystemPrompt:
    def test_contains_identity_section(self):
        prompt = build_system_prompt(cwd=_TMP)
        assert "AI" in prompt or "assistant" in prompt

    def test_contains_environment_section(self):
        prompt = build_system_prompt(cwd=_TMP)
        assert _TMP in prompt

    def test_contains_tools_section(self):
        prompt = build_system_prompt(cwd=_TMP)
        assert "ReadFile" in prompt or "read_file" in prompt.lower()

    def test_contains_dynamic_boundary(self):
        prompt = build_system_prompt(cwd=_TMP)
        assert DYNAMIC_BOUNDARY in prompt

    def test_contains_output_style(self):
        prompt = build_system_prompt(cwd=_TMP)
        assert "concise" in prompt.lower() or "brief" in prompt.lower()

    def test_memory_section_included_when_content(self):
        prompt = build_system_prompt(cwd=_TMP, memory_content="Remember: user prefers Python")
        assert "user prefers Python" in prompt

    def test_memory_section_absent_when_empty(self):
        prompt = build_system_prompt(cwd=_TMP, memory_content="")
        # When no memory content, no Memory section header should appear
        # The output_style section will still be in dynamic zone, so DYNAMIC_BOUNDARY exists
        lines = prompt.split("\n")
        memory_lines = [line for line in lines if line.strip().startswith("# Memory")]
        assert len(memory_lines) == 0


class TestSplitByDynamicBoundary:
    def test_splits_at_boundary(self):
        prompt = f"STATIC PART\n\n{DYNAMIC_BOUNDARY}\n\nDYNAMIC PART"
        static, dynamic = split_by_dynamic_boundary(prompt)
        assert static == "STATIC PART"
        assert dynamic == "DYNAMIC PART"

    def test_no_boundary_returns_full_as_static(self):
        prompt = "Full prompt without boundary"
        static, dynamic = split_by_dynamic_boundary(prompt)
        assert static == prompt
        assert dynamic == ""

    def test_empty_dynamic_part(self):
        prompt = f"STATIC\n\n{DYNAMIC_BOUNDARY}"
        static, dynamic = split_by_dynamic_boundary(prompt)
        assert static == "STATIC"
        assert dynamic == ""

    def test_roundtrip_with_builder(self):
        builder = SystemPromptBuilder()
        builder.add_cached_section("s1", lambda: "STATIC_A", priority=100, is_static=True)
        builder.add_cached_section("s2", lambda: "DYNAMIC_B", priority=50, is_static=False)
        full = builder.build()
        static, dynamic = split_by_dynamic_boundary(full)
        assert "STATIC_A" in static
        assert "DYNAMIC_B" in dynamic
        assert DYNAMIC_BOUNDARY not in static
        assert DYNAMIC_BOUNDARY not in dynamic


class TestBuildCloudConfigSection:
    def test_returns_empty_when_no_providers(self):
        with patch("iac_code.services.cloud_credentials.CloudCredentials") as mock_cls:
            mock_cls.return_value.list_providers.return_value = []
            assert _build_cloud_config_section() == ""

    def test_returns_region_for_aliyun(self):
        with patch("iac_code.services.cloud_credentials.CloudCredentials") as mock_cls:
            from iac_code.services.providers.aliyun import AliyunCredential

            mock_instance = mock_cls.return_value
            mock_instance.list_providers.return_value = ["aliyun"]
            mock_instance.get_provider.return_value = AliyunCredential(region_id="cn-shanghai")
            result = _build_cloud_config_section()
            assert "# Cloud Configuration" in result
            assert "cn-shanghai" in result
            assert "Alibaba Cloud" in result

    def test_returns_empty_on_exception(self):
        with patch(
            "iac_code.services.cloud_credentials.CloudCredentials",
            side_effect=Exception("fail"),
        ):
            assert _build_cloud_config_section() == ""


class TestReadGitHead:
    """Tests for the shared ``_read_git_head`` helper used by system prompt."""

    def test_non_repo_returns_false(self, tmp_path: Path):
        is_repo, head = _read_git_head(str(tmp_path))
        assert is_repo is False
        assert head == ""

    def test_repo_with_branch(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        is_repo, head = _read_git_head(str(tmp_path))
        assert is_repo is True
        assert head == "ref: refs/heads/main"

    def test_detached_head_returns_sha(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        sha = "abcdef0123456789abcdef0123456789abcdef01"
        (git_dir / "HEAD").write_text(f"{sha}\n", encoding="utf-8")
        is_repo, head = _read_git_head(str(tmp_path))
        assert is_repo is True
        assert head == sha


class TestBuildEnvironmentSectionGit:
    """Verify ``_build_environment_section`` shows git info without subprocess."""

    def test_shows_branch(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/my-branch\n", encoding="utf-8")
        section = _build_environment_section(str(tmp_path))
        assert "Git repository: True" in section
        assert "Git branch: my-branch" in section

    def test_detached_head_shows_short_sha(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("abcdef0123456789abcdef0123456789abcdef01\n", encoding="utf-8")
        section = _build_environment_section(str(tmp_path))
        assert "Git repository: True" in section
        assert "Git branch: abcdef0" in section

    def test_non_repo(self, tmp_path: Path):
        section = _build_environment_section(str(tmp_path))
        assert "Git repository: False" in section
        assert "Git branch" not in section

    def test_no_subprocess_call(self, tmp_path: Path):
        """Hard guarantee: environment section never invokes subprocess."""
        with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            _build_environment_section(str(tmp_path))
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
