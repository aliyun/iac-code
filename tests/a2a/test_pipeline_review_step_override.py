"""售卖流水线「审查步骤」持久化开关注入 fresh feature flags 的优先级测试。

优先级:pipeline.yaml default < settings.yml 开关 < 显式 env var。
只影响 fresh 运行(resume 走冻结的 sidecar 元数据,不经过此路径)。
"""

from unittest.mock import patch

from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

_apply = IacCodeA2APipelineExecutor._apply_persisted_feature_flag_overrides

_RAW_FLAGS = {
    "enable_reviewing": {
        "env": "IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING",
        "default": False,
    }
}


class TestApplyPersistedFeatureFlagOverrides:
    def test_flag_absent_is_noop(self):
        flags: dict[str, bool] = {"other_flag": True}
        _apply(flags, _RAW_FLAGS)
        assert flags == {"other_flag": True}

    def test_setting_on_flips_default_off(self, monkeypatch):
        monkeypatch.delenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", raising=False)
        flags = {"enable_reviewing": False}
        with patch(
            "iac_code.a2a.pipeline_executor.is_selling_review_step_enabled",
            return_value=True,
        ):
            _apply(flags, _RAW_FLAGS)
        assert flags["enable_reviewing"] is True

    def test_setting_off_keeps_default_off(self, monkeypatch):
        monkeypatch.delenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", raising=False)
        flags = {"enable_reviewing": False}
        with patch(
            "iac_code.a2a.pipeline_executor.is_selling_review_step_enabled",
            return_value=False,
        ):
            _apply(flags, _RAW_FLAGS)
        assert flags["enable_reviewing"] is False

    def test_explicit_env_wins_over_setting(self, monkeypatch):
        # env 显式关闭:即便持久化开关打开,也保持 env 解析出的 False。
        monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "false")
        flags = {"enable_reviewing": False}
        with patch(
            "iac_code.a2a.pipeline_executor.is_selling_review_step_enabled",
            return_value=True,
        ) as reader:
            _apply(flags, _RAW_FLAGS)
        assert flags["enable_reviewing"] is False
        reader.assert_not_called()

    def test_unrecognized_env_value_does_not_win(self, monkeypatch):
        # env 存在但非可识别布尔值时,不视为显式设置,持久化开关照常生效。
        monkeypatch.setenv("IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING", "maybe")
        flags = {"enable_reviewing": False}
        with patch(
            "iac_code.a2a.pipeline_executor.is_selling_review_step_enabled",
            return_value=True,
        ):
            _apply(flags, _RAW_FLAGS)
        assert flags["enable_reviewing"] is True

    def test_no_env_spec_falls_back_to_setting(self, monkeypatch):
        # feature flag 无 env 字段时也应用持久化开关。
        flags = {"enable_reviewing": False}
        with patch(
            "iac_code.a2a.pipeline_executor.is_selling_review_step_enabled",
            return_value=True,
        ):
            _apply(flags, {"enable_reviewing": {"default": False}})
        assert flags["enable_reviewing"] is True
