from unittest.mock import patch

from iac_code.pipeline.config import (
    RunMode,
    get_pipeline_name,
    get_run_mode,
    get_working_directory,
    is_selling_review_step_enabled,
    save_selling_review_step_enabled,
)


class TestRunMode:
    def test_default_is_normal(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_run_mode() == RunMode.NORMAL

    def test_pipeline_mode(self):
        with patch.dict("os.environ", {"IAC_CODE_MODE": "pipeline"}):
            assert get_run_mode() == RunMode.PIPELINE

    def test_case_insensitive(self):
        with patch.dict("os.environ", {"IAC_CODE_MODE": "Pipeline"}):
            assert get_run_mode() == RunMode.PIPELINE

    def test_invalid_falls_back_to_normal(self):
        with patch.dict("os.environ", {"IAC_CODE_MODE": "unknown"}):
            assert get_run_mode() == RunMode.NORMAL


class TestPipelineName:
    def test_default_is_selling(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_pipeline_name() == "selling"

    def test_env_override(self):
        with patch.dict("os.environ", {"IAC_CODE_PIPELINE_NAME": "custom"}):
            assert get_pipeline_name() == "custom"


class TestWorkingDirectory:
    def test_default_is_none(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_working_directory() is None

    def test_env_override(self):
        with patch.dict("os.environ", {"IAC_CODE_CWD": "/tmp/my-project"}):
            assert get_working_directory() == "/tmp/my-project"

    def test_empty_string_returns_none(self):
        with patch.dict("os.environ", {"IAC_CODE_CWD": ""}):
            assert get_working_directory() is None


class TestSellingReviewStep:
    def test_default_is_false_when_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        assert is_selling_review_step_enabled() is False

    def test_save_then_read_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        assert save_selling_review_step_enabled(True) is True
        assert is_selling_review_step_enabled() is True
        assert save_selling_review_step_enabled(False) is False
        assert is_selling_review_step_enabled() is False

    def test_save_coerces_truthy_to_bool(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        assert save_selling_review_step_enabled(1) is True
        assert is_selling_review_step_enabled() is True

    def test_save_preserves_other_pipeline_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        from iac_code.config import _load_yaml, _save_yaml, get_settings_path

        path = get_settings_path()
        data = _load_yaml(path)
        data.setdefault("pipeline", {})["someOtherKey"] = "keep-me"
        _save_yaml(path, data)

        save_selling_review_step_enabled(True)

        reloaded = _load_yaml(get_settings_path())
        assert reloaded["pipeline"]["someOtherKey"] == "keep-me"
        assert reloaded["pipeline"]["sellingReviewStep"] is True

    def test_read_ignores_non_dict_section(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        from iac_code.config import _load_yaml, _save_yaml, get_settings_path

        path = get_settings_path()
        data = _load_yaml(path)
        data["pipeline"] = "not-a-dict"
        _save_yaml(path, data)

        assert is_selling_review_step_enabled() is False
