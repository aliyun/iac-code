from unittest.mock import patch

from iac_code.agent.system_prompt import (
    DYNAMIC_BOUNDARY,
    SystemPromptBuilder,
    _build_cloud_config_section,
    build_system_prompt,
)


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
        prompt = build_system_prompt(cwd="/tmp")
        assert "AI" in prompt or "assistant" in prompt

    def test_contains_environment_section(self):
        prompt = build_system_prompt(cwd="/tmp/test")
        assert "/tmp/test" in prompt

    def test_contains_tools_section(self):
        prompt = build_system_prompt(cwd="/tmp")
        assert "ReadFile" in prompt or "read_file" in prompt.lower()

    def test_contains_dynamic_boundary(self):
        prompt = build_system_prompt(cwd="/tmp")
        assert DYNAMIC_BOUNDARY in prompt

    def test_contains_output_style(self):
        prompt = build_system_prompt(cwd="/tmp")
        assert "concise" in prompt.lower() or "brief" in prompt.lower()

    def test_memory_section_included_when_content(self):
        prompt = build_system_prompt(cwd="/tmp", memory_content="Remember: user prefers Python")
        assert "user prefers Python" in prompt

    def test_memory_section_absent_when_empty(self):
        prompt = build_system_prompt(cwd="/tmp", memory_content="")
        # When no memory content, no Memory section header should appear
        # The output_style section will still be in dynamic zone, so DYNAMIC_BOUNDARY exists
        lines = prompt.split("\n")
        memory_lines = [line for line in lines if line.strip().startswith("# Memory")]
        assert len(memory_lines) == 0


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
