from unittest.mock import patch

from iac_code.pipeline.config import (
    RunMode,
    get_pipeline_name,
    get_run_mode,
    get_working_directory,
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
