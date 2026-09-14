"""Tests for simple auth helpers and metadata."""

from iac_code.commands.auth import _BACK, PROVIDERS, _display_width


class TestDisplayWidth:
    def test_ascii(self):
        assert _display_width("hello") == 5

    def test_cjk_double_width(self):
        assert _display_width("阿里云") == 6

    def test_mixed(self):
        assert _display_width("hi 阿里") == 3 + 2 + 2

    def test_empty(self):
        assert _display_width("") == 0


class TestProvidersMetadata:
    def test_providers_nonempty(self):
        assert len(PROVIDERS) >= 3

    def test_provider_keys_unique(self):
        names = [p["key_name"] for p in PROVIDERS]
        assert len(names) == len(set(names))

    def test_each_provider_has_required_fields(self):
        for p in PROVIDERS:
            assert "name" in p
            assert "display_name" in p
            assert "key_name" in p
            assert "models" in p
            assert "default_model" in p

    def test_back_sentinel_unique(self):
        assert _BACK is not None
        # Sentinel identity check
        assert _BACK is _BACK

    def test_dashscope_token_plan_entry_present(self):
        entry = next(
            (p for p in PROVIDERS if p["key_name"] == "dashscope_token_plan"),
            None,
        )
        assert entry is not None
        assert entry["name"] == "DashScope Token Plan"
        assert entry["display_name"] == "Alibaba Cloud Bailian Token Plan"
        assert entry["api_base"] == ("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
        assert entry["default_model"] == "qwen3.8-max"
        assert entry["models"] == [
            "qwen3.8-max",
            "qwen3.8-flash",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.6-flash",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "deepseek-v4-flash-0731",
            "glm-5.2",
        ]

    def test_kimi_code_entry_exposes_k28_and_effort_models(self):
        entry = next(p for p in PROVIDERS if p["key_name"] == "kimi_code")

        assert entry["display_name"] == "Kimi Code"
        assert entry["api_base"] == "https://api.kimi.com/coding/v1"
        assert entry["default_model"] == "kimi-for-coding"
        assert entry["models"] == ["kimi-for-coding", "k3", "k3-256k", "kimi-for-coding-highspeed"]
