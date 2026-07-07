from __future__ import annotations

from typer.testing import CliRunner

from iac_code.cli.main import app


def test_help_includes_input_format_option() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--input-format" in result.output


def test_process_mode_rejects_prompt_combination() -> None:
    result = CliRunner().invoke(
        app,
        ["--input-format", "stream-json", "--output-format", "stream-json", "--prompt", "hello"],
    )

    assert result.exit_code == 1
    assert "--prompt cannot be used with --input-format stream-json" in result.output


def test_process_mode_requires_stream_json_output() -> None:
    result = CliRunner().invoke(app, ["--input-format", "stream-json", "--output-format", "json"])

    assert result.exit_code == 1
    assert "--input-format stream-json requires --output-format stream-json" in result.output


def test_process_mode_rejects_invalid_input_format() -> None:
    result = CliRunner().invoke(app, ["--input-format", "yaml"])

    assert result.exit_code == 1
    assert "Invalid --input-format 'yaml'" in result.output


def test_process_mode_invokes_runner_in_pipeline_mode(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeProcessModeRunner:
        def __init__(self, options) -> None:
            captured["options"] = options

        async def run(self) -> int:
            captured["ran"] = True
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setattr("iac_code.cli.process_mode.ProcessModeRunner", FakeProcessModeRunner)

    result = CliRunner().invoke(app, ["--input-format", "stream-json", "--output-format", "stream-json"])

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["options"].cwd == str(tmp_path)
    assert captured["options"].run_mode == "pipeline"


def test_process_mode_invokes_runner(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeProcessModeRunner:
        def __init__(self, options) -> None:
            captured["options"] = options

        async def run(self) -> int:
            captured["ran"] = True
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.setattr("iac_code.cli.process_mode.ProcessModeRunner", FakeProcessModeRunner)

    result = CliRunner().invoke(app, ["--input-format", "stream-json", "--output-format", "stream-json"])

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["options"].cwd == str(tmp_path)


def test_process_mode_unknown_iac_code_mode_falls_back_to_normal(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeProcessModeRunner:
        def __init__(self, options) -> None:
            captured["options"] = options

        async def run(self) -> int:
            captured["ran"] = True
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_MODE", "unexpected")
    monkeypatch.setattr("iac_code.cli.process_mode.ProcessModeRunner", FakeProcessModeRunner)

    result = CliRunner().invoke(app, ["--input-format", "stream-json", "--output-format", "stream-json"])

    assert result.exit_code == 0
    assert captured["ran"] is True
    assert captured["options"].cwd == str(tmp_path)
