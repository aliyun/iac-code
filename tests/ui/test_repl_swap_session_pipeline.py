"""Regression tests for /resume swap during pipeline mode (问题 4)."""

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.prerequisites import (
    InstallerSpec,
    PrerequisiteDecision,
    PrerequisiteProgress,
    PrerequisiteResolution,
)
from iac_code.pipeline.engine.user_input import PipelineUserInput


def _make_repl_with_pipeline(tmp_path: Path, session_id_old: str, session_id_new: str):
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_waiting_input = True
    repl._runtime_mode = RunMode.PIPELINE
    repl._session_id = session_id_old
    repl._original_cwd = "/proj"
    repl._was_resumed = False
    repl._agent_loop = MagicMock()
    repl._agent_loop.replace_session = MagicMock()
    repl.console = MagicMock()
    repl.console.file.write = MagicMock()
    repl.console.file.flush = MagicMock()
    repl.console.print = MagicMock()
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl._load_current_session_name = MagicMock(return_value=None)
    repl.swap_session = InlineREPL.swap_session.__get__(repl)
    repl._set_runtime_mode = InlineREPL._set_runtime_mode.__get__(repl)
    repl._prepare_pipeline_prerequisite_metadata = InlineREPL._prepare_pipeline_prerequisite_metadata.__get__(repl)
    repl._sidecar_prerequisite_metadata = InlineREPL._sidecar_prerequisite_metadata.__get__(repl)
    repl._load_pipeline_raw_config = InlineREPL._load_pipeline_raw_config.__get__(repl)
    repl._apply_pipeline_prerequisite_env_overrides = InlineREPL._apply_pipeline_prerequisite_env_overrides
    repl._print_pipeline_prerequisite_status_messages = InlineREPL._print_pipeline_prerequisite_status_messages.__get__(
        repl
    )
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)

    sessions_root = tmp_path / "projects" / "proj"
    sessions_root.mkdir(parents=True)

    storage = MagicMock()
    storage.session_dir.side_effect = lambda cwd, sid: sessions_root / sid
    storage.session_path.side_effect = lambda cwd, sid: sessions_root / sid / "session.jsonl"
    storage.load.return_value = []
    storage.repair_interrupted.side_effect = lambda msgs: msgs

    repl._session_storage = storage
    return repl, sessions_root


def _write_pipeline_yaml(pipeline_dir: Path) -> None:
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "pipeline.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "test-pipeline",
                "feature_flags": {"reviewing": {"default": True, "display_key": "review_step"}},
                "prerequisites": {
                    "infraguard": {
                        "command": "infraguard",
                        "required_by_flags": ["reviewing"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _make_repl_for_pipeline_chat(tmp_path: Path):
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = None
    repl._pipeline_waiting_input = False
    repl._pipeline_restored_status = None
    repl._pipeline_state_persistence_failed = False
    repl._session_id = "session-1"
    repl._original_cwd = str(tmp_path)
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._session_storage = MagicMock()
    repl._session_storage.session_dir.side_effect = lambda cwd, sid: tmp_path / sid
    repl.store = MagicMock()
    repl.store.get_state.return_value = SimpleNamespace(permission_context=None)
    repl.console = MagicMock()
    repl.console.print = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._pipeline_memory_content_getter = MagicMock(return_value=lambda: "")
    repl._refresh_pipeline_display_recorder = MagicMock()
    repl._detect_pipeline_session = MagicMock(return_value=False)
    repl._persist_pipeline_visible_user_turn = MagicMock()
    repl._render_pipeline_stream = AsyncMock(return_value=None)
    repl._finalize_pipeline_after_render = MagicMock()
    repl._flush_pipeline_telemetry = AsyncMock()
    repl._maybe_start_pipeline_cleanup = AsyncMock()
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    repl._prepare_pipeline_prerequisite_metadata = InlineREPL._prepare_pipeline_prerequisite_metadata.__get__(repl)
    repl._sidecar_prerequisite_metadata = InlineREPL._sidecar_prerequisite_metadata.__get__(repl)
    repl._load_pipeline_raw_config = InlineREPL._load_pipeline_raw_config.__get__(repl)
    repl._apply_pipeline_prerequisite_env_overrides = InlineREPL._apply_pipeline_prerequisite_env_overrides
    repl._print_pipeline_prerequisite_status_messages = InlineREPL._print_pipeline_prerequisite_status_messages.__get__(
        repl
    )
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)
    return repl


class _FakePipeline:
    sidecar_restore_result = None

    def run(self, _pipeline_input):
        return _empty_stream()


def test_pipeline_prerequisite_choice_announces_missing_required_review_prerequisite(monkeypatch):
    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl.renderer = MagicMock()
    repl._pipeline_prerequisite_required_flags_by_name = {"infraguard": ["enable_reviewing"]}
    repl._pipeline_prerequisite_feature_labels_by_flag = {"enable_reviewing": "review step"}
    repl._pipeline_prerequisite_choice = InlineREPL._pipeline_prerequisite_choice.__get__(repl)
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)
    repl._pipeline_prerequisite_feature_label = InlineREPL._pipeline_prerequisite_feature_label.__get__(repl)

    class FakeSelect:
        def __init__(self, options, default_value=None, layout=None):
            self.options = options
            self.default_value = default_value
            self.layout = layout

        def run(self, **kwargs):
            return "skip"

    monkeypatch.setattr(repl_module, "Select", FakeSelect)

    result = repl._pipeline_prerequisite_choice(
        "infraguard",
        [
            InstallerSpec(
                id="go-install",
                platforms=["darwin", "linux", "windows"],
                requires_commands=["go"],
            )
        ],
    )

    assert result is None
    messages = [call.args[0] for call in repl.renderer.print_system_message.call_args_list]
    assert any("infraguard" in message and "review" in message and "missing" in message.lower() for message in messages)


def test_pipeline_prerequisite_choice_uses_translated_installer_labels_and_repl_console(monkeypatch):
    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl.console = MagicMock()
    repl.renderer = MagicMock()
    repl._pipeline_prerequisite_required_flags_by_name = {"infraguard": ["enable_reviewing"]}
    repl._pipeline_prerequisite_feature_labels_by_flag = {"enable_reviewing": "review step"}
    repl._pipeline_prerequisite_choice = InlineREPL._pipeline_prerequisite_choice.__get__(repl)
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)
    repl._pipeline_prerequisite_feature_label = InlineREPL._pipeline_prerequisite_feature_label.__get__(repl)

    captured = {}

    class FakeSelect:
        def __init__(self, options, default_value=None, layout=None):
            captured["options"] = options
            captured["default_value"] = default_value
            captured["layout"] = layout

        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return "go-install"

    monkeypatch.setattr(repl_module, "Select", FakeSelect)

    result = repl._pipeline_prerequisite_choice(
        "infraguard",
        [
            InstallerSpec(id="direct-binary", platforms=["darwin"], display_key="direct_binary_download"),
            InstallerSpec(id="homebrew", platforms=["darwin"], display_key="homebrew"),
            InstallerSpec(id="go-install", platforms=["darwin"], display_key="go_install"),
        ],
    )

    assert result == "go-install"
    labels = [option.label for option in captured["options"]]
    assert labels[:3] == ["Direct binary download", "Homebrew", "Go install"]
    assert "direct-binary" not in labels
    assert captured["run_kwargs"] == {"console": repl.console}


def test_pipeline_prerequisite_choice_uses_generic_feature_label_for_non_review_prerequisite(monkeypatch):
    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl.renderer = MagicMock()
    repl._pipeline_prerequisite_required_flags_by_name = {"custom_tool": ["enable_custom_feature"]}
    repl._pipeline_prerequisite_choice = InlineREPL._pipeline_prerequisite_choice.__get__(repl)
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)
    repl._pipeline_prerequisite_feature_label = InlineREPL._pipeline_prerequisite_feature_label.__get__(repl)

    class FakeSelect:
        def __init__(self, options, default_value=None, layout=None):
            self.options = options

        def run(self, **kwargs):
            return "skip"

    monkeypatch.setattr(repl_module, "Select", FakeSelect)

    result = repl._pipeline_prerequisite_choice(
        "custom_tool",
        [
            InstallerSpec(
                id="custom-install",
                platforms=["linux"],
            )
        ],
    )

    assert result is None
    messages = [call.args[0] for call in repl.renderer.print_system_message.call_args_list]
    assert any("configured feature" in message for message in messages)
    assert all("review" not in message.lower() for message in messages)


def test_pipeline_prerequisite_choice_uses_configured_feature_label(monkeypatch):
    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl.renderer = MagicMock()
    repl._pipeline_prerequisite_required_flags_by_name = {"cost_tool": ["enable_cost_estimation"]}
    repl._pipeline_prerequisite_feature_labels_by_flag = {"enable_cost_estimation": "cost estimation"}
    repl._pipeline_prerequisite_choice = InlineREPL._pipeline_prerequisite_choice.__get__(repl)
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)
    repl._pipeline_prerequisite_feature_label = InlineREPL._pipeline_prerequisite_feature_label.__get__(repl)

    class FakeSelect:
        def __init__(self, options, default_value=None, layout=None):
            self.options = options

        def run(self, **kwargs):
            return "skip"

    monkeypatch.setattr(repl_module, "Select", FakeSelect)

    result = repl._pipeline_prerequisite_choice(
        "cost_tool",
        [
            InstallerSpec(
                id="cost-install",
                platforms=["linux"],
            )
        ],
    )

    assert result is None
    messages = [call.args[0] for call in repl.renderer.print_system_message.call_args_list]
    assert any("cost estimation" in message for message in messages)
    assert all("configured feature" not in message for message in messages)
    assert all("review" not in message.lower() for message in messages)


def test_pipeline_prerequisite_status_message_uses_configured_feature_label():
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl.renderer = MagicMock()
    repl.console = MagicMock()
    repl._pipeline_prerequisite_feature_labels_by_flag = {"enable_cost_estimation": "cost estimation"}
    repl._print_pipeline_prerequisite_status_messages = InlineREPL._print_pipeline_prerequisite_status_messages.__get__(
        repl
    )
    repl._pipeline_prerequisite_feature_name = InlineREPL._pipeline_prerequisite_feature_name.__get__(repl)

    repl._print_pipeline_prerequisite_status_messages(
        {
            "feature_flags": {"enable_cost_estimation": False},
            "decisions": {
                "cost_tool": {
                    "status": "install_failed",
                    "required_flags": ["enable_cost_estimation"],
                    "message": "install failed",
                }
            },
        }
    )

    messages = [call.args[0] for call in repl.renderer.print_system_message.call_args_list]
    assert any("cost estimation skipped" in message for message in messages)
    assert all("Review step skipped" not in message for message in messages)


def test_pipeline_prerequisite_progress_display_refreshes_without_new_output(monkeypatch):
    from iac_code.ui import repl as repl_module

    updates = []

    class FakeRenderer:
        def __init__(self, console):
            self.console = console

        def render(self, renderable):
            updates.append(("render", renderable))

        def clear(self, **kwargs):
            updates.append(("clear", kwargs))

    monkeypatch.setattr(repl_module, "InPlaceRenderer", FakeRenderer)

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=100), refresh_interval=0.01)
    display.handle(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="homebrew",
            phase="install",
            status="started",
            message="Running brew tap",
            command=["brew", "tap", "aliyun/infraguard"],
        )
    )
    time.sleep(0.05)
    display.close()

    render_updates = [update for update in updates if update[0] == "render"]
    assert len(render_updates) >= 2
    assert updates[-1] == ("clear", {"clear_to_screen_end": True})


def test_pipeline_prerequisite_progress_display_can_resume_after_clear(monkeypatch):
    from iac_code.ui import repl as repl_module

    updates = []

    class FakeRenderer:
        def __init__(self, console):
            self.console = console

        def render(self, renderable):
            updates.append(("render", renderable))

        def clear(self, **kwargs):
            updates.append(("clear", kwargs))

    monkeypatch.setattr(repl_module, "InPlaceRenderer", FakeRenderer)

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=100), refresh_interval=10)
    display.handle(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="go-install",
            phase="path_hint",
            status="output",
            message="/Users/ehzyo/go",
        )
    )
    display.clear()
    display.handle(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="go-install",
            phase="install",
            status="started",
            message="Installing infraguard",
        )
    )
    display.close()

    assert [update[0] for update in updates] == ["render", "clear", "render", "clear"]
    assert updates[1] == ("clear", {"clear_to_screen_end": True})
    assert updates[3] == ("clear", {"clear_to_screen_end": True})


def test_pipeline_prerequisite_progress_display_formats_human_readable_lines():
    from iac_code.ui import repl as repl_module

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=110))

    started = display._format_event(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="homebrew",
            phase="install",
            status="started",
            message="Running brew tap aliyun/infraguard https://github.com/aliyun/infraguard",
            command=["brew", "tap", "aliyun/infraguard", "https://github.com/aliyun/infraguard"],
        )
    )
    output = display._format_event(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="homebrew",
            phase="install",
            status="output",
            message="==> Tapping aliyun/infraguard",
            command=["brew", "tap", "aliyun/infraguard", "https://github.com/aliyun/infraguard"],
        )
    )

    assert started == "Running: brew tap aliyun/infraguard ..."
    assert output == "==> Tapping aliyun/infraguard"
    assert "install:output" not in output
    assert "[homebrew]" not in started


def test_pipeline_prerequisite_progress_display_formats_download_progress():
    from iac_code.ui import repl as repl_module

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=110))

    known_total = display._format_event(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="direct-binary",
            phase="download",
            status="output",
            message="Downloading infraguard-v0.10.0-darwin-arm64: 40% (8.0 MB / 20.0 MB)",
            command=["download", "infraguard-v0.10.0-darwin-arm64"],
            downloaded_bytes=8 * 1024 * 1024,
            total_bytes=20 * 1024 * 1024,
        )
    )
    unknown_total = display._format_event(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="direct-binary",
            phase="download",
            status="output",
            message="Downloading infraguard-v0.10.0-darwin-arm64: 1.5 MB downloaded",
            command=["download", "infraguard-v0.10.0-darwin-arm64"],
            downloaded_bytes=1536 * 1024,
        )
    )

    assert known_total == "Download: 40% (8.0 MB / 20.0 MB)"
    assert unknown_total == "Download: 1.5 MB downloaded"


def test_pipeline_prerequisite_progress_display_uses_translated_installer_label():
    from iac_code.ui import repl as repl_module

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=110))

    status = display._status_for_event(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="go-install",
            installer_display_key="go_install",
            phase="install",
            status="started",
            message="Installing infraguard",
        )
    )

    assert status == "Installing prerequisites with Go install..."
    assert "go-install" not in status


def test_pipeline_prerequisite_progress_display_updates_download_progress_in_place():
    from iac_code.ui import repl as repl_module

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=110))

    for downloaded in (1, 2, 3):
        display.handle(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="direct-binary",
                phase="download",
                status="output",
                message=f"Downloading infraguard-v0.10.0-darwin-arm64: {downloaded} MB",
                command=["download", "infraguard-v0.10.0-darwin-arm64"],
                downloaded_bytes=downloaded * 1024 * 1024,
                total_bytes=54 * 1024 * 1024,
            )
        )

    rendered = display._render().renderables[1].plain
    display.close()

    assert "Download: 6% (3.0 MB / 54.0 MB)" in rendered
    assert "Download: 2%" not in rendered
    assert "Download: 4%" not in rendered


def test_pipeline_prerequisite_progress_display_hides_stale_path_hint_during_download():
    from iac_code.ui import repl as repl_module

    display = repl_module._PipelinePrerequisiteProgressDisplay(MagicMock(width=110))
    display.handle(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="go-install",
            phase="path_hint",
            status="output",
            message="/Users/ehzyo/go",
        )
    )
    display.handle(
        PrerequisiteProgress(
            name="infraguard",
            installer_id="direct-binary",
            phase="download",
            status="output",
            message="Downloading infraguard-v0.10.1-darwin-arm64: 9% (5.0 MB / 55.1 MB)",
            command=["download", "infraguard-v0.10.1-darwin-arm64"],
            downloaded_bytes=5 * 1024 * 1024,
            total_bytes=55 * 1024 * 1024,
        )
    )

    rendered = display._render().renderables[1].plain
    display.close()

    assert "Download: 9%" in rendered
    assert "/Users/ehzyo/go" not in rendered


def test_pipeline_prerequisite_prepare_installs_immediate_sigint_handler(tmp_path, monkeypatch):
    from iac_code.ui import repl as repl_module

    repl = _make_repl_for_pipeline_chat(tmp_path)
    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    progress_display = MagicMock()
    installed_handlers = []
    previous_handler = object()
    wakeup_fds = []

    repl._load_pipeline_raw_config = MagicMock(
        return_value={
            "feature_flags": {"reviewing": {"default": True}},
            "prerequisites": {
                "infraguard": {
                    "command": "infraguard",
                    "required_by_flags": ["reviewing"],
                }
            },
        }
    )
    repl._make_pipeline_prerequisite_progress_display = MagicMock(return_value=progress_display)
    repl._pipeline_prerequisite_choice = MagicMock()
    original_signal = repl_module.signal.signal
    original_getsignal = repl_module.signal.getsignal

    def fake_prepare(*args, **kwargs):
        assert installed_handlers
        installed_handlers[-1](repl_module.signal.SIGINT, None)
        raise AssertionError("SIGINT handler should raise KeyboardInterrupt")

    def fake_signal(signum, handler):
        if signum != repl_module.signal.SIGINT:
            return original_signal(signum, handler)
        installed_handlers.append(handler)
        return previous_handler

    monkeypatch.setattr(repl_module, "prepare_prerequisites", fake_prepare)
    monkeypatch.setattr(
        repl_module.signal,
        "getsignal",
        lambda signum: previous_handler if signum == repl_module.signal.SIGINT else original_getsignal(signum),
    )
    monkeypatch.setattr(repl_module.signal, "signal", fake_signal)
    monkeypatch.setattr(repl_module.signal, "set_wakeup_fd", lambda fd: wakeup_fds.append(fd) or 42)

    with pytest.raises(KeyboardInterrupt):
        repl._prepare_pipeline_prerequisite_metadata(
            pipeline_name="test-pipeline",
            cwd=str(tmp_path),
            session_id="session-1",
        )

    assert installed_handlers[-1] is previous_handler
    assert wakeup_fds == [-1, 42]
    progress_display.close.assert_called_once()


def test_pipeline_prerequisite_prepare_clears_progress_before_installer_choice(tmp_path, monkeypatch):
    from iac_code.ui import repl as repl_module

    repl = _make_repl_for_pipeline_chat(tmp_path)
    order = []
    resolution = PrerequisiteResolution(
        feature_flags={"reviewing": False},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status="declined_or_unavailable",
                required_flags=["reviewing"],
            )
        },
    )

    class FakeProgressDisplay:
        def handle(self, event):
            order.append(("handle", event.phase))

        def clear(self):
            order.append(("clear", None))

        def close(self):
            order.append(("close", None))

    def fake_prepare(raw_prerequisites, *, feature_flags, surface, choose_installer, progress_handler=None):
        assert progress_handler is not None
        progress_handler(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="go-install",
                phase="path_hint",
                status="output",
                message="/Users/ehzyo/go",
            )
        )
        choose_installer("infraguard", [])
        return resolution

    def fake_choice(name, installers):
        order.append(("choice", name))
        return None

    repl._load_pipeline_raw_config = MagicMock(
        return_value={
            "feature_flags": {"reviewing": {"default": True}},
            "prerequisites": {
                "infraguard": {
                    "command": "infraguard",
                    "required_by_flags": ["reviewing"],
                }
            },
        }
    )
    repl._make_pipeline_prerequisite_progress_display = MagicMock(return_value=FakeProgressDisplay())
    repl._pipeline_prerequisite_choice = fake_choice
    monkeypatch.setattr(repl_module, "prepare_prerequisites", fake_prepare)

    repl._prepare_pipeline_prerequisite_metadata(
        pipeline_name="test-pipeline",
        cwd=str(tmp_path),
        session_id="session-1",
    )

    assert order == [
        ("handle", "path_hint"),
        ("clear", None),
        ("choice", "infraguard"),
        ("close", None),
    ]


@pytest.mark.asyncio
async def test_handle_pipeline_chat_prepares_prerequisites_before_create(tmp_path, monkeypatch):
    from iac_code.ui import repl as repl_module

    repl = _make_repl_for_pipeline_chat(tmp_path)
    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    resolution = PrerequisiteResolution(
        feature_flags={"reviewing": False},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status="declined_or_unavailable",
                required_flags=["reviewing"],
            )
        },
        env_overrides={"INFRAGUARD_PATH": "/tmp/infraguard"},
    )
    prepare_calls = []
    create_kwargs = {}
    choice_calls = []

    def choice(name, installers):
        choice_calls.append((name, installers))
        return None

    repl._pipeline_prerequisite_choice = choice

    def fake_prepare(raw_prerequisites, *, feature_flags, surface, choose_installer, progress_handler=None):
        assert progress_handler is not None
        progress_handler(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="go-install",
                phase="install",
                status="started",
                message="Installing infraguard",
            )
        )
        prepare_calls.append(
            {
                "raw_prerequisites": raw_prerequisites,
                "feature_flags": feature_flags,
                "surface": surface,
                "choose_installer": choose_installer,
                "progress_handler": progress_handler,
            }
        )
        return resolution

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return _FakePipeline()

    monkeypatch.delenv("INFRAGUARD_PATH", raising=False)
    monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr("iac_code.pipeline.config.get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: None)
    monkeypatch.setattr(repl_module, "prepare_prerequisites", fake_prepare, raising=False)
    monkeypatch.setattr("iac_code.pipeline.create_pipeline", fake_create_pipeline)

    await repl._handle_pipeline_chat("deploy")

    assert prepare_calls[0]["raw_prerequisites"] == {
        "infraguard": {
            "command": "infraguard",
            "required_by_flags": ["reviewing"],
        }
    }
    assert prepare_calls[0]["feature_flags"] == {"reviewing": True}
    assert prepare_calls[0]["surface"] == "repl"
    assert prepare_calls[0]["progress_handler"] is not None
    assert prepare_calls[0]["choose_installer"] is not choice
    assert prepare_calls[0]["choose_installer"]("infraguard", []) is None
    assert choice_calls == [("infraguard", [])]
    repl._make_pipeline_prerequisite_progress_display.return_value.clear.assert_called_once()
    assert create_kwargs["prerequisite_resolution"] == resolution.to_metadata()
    assert "INFRAGUARD_PATH" not in os.environ


@pytest.mark.asyncio
async def test_handle_pipeline_chat_reports_disabled_review_prerequisite_status(tmp_path, monkeypatch):
    from iac_code.ui import repl as repl_module

    repl = _make_repl_for_pipeline_chat(tmp_path)
    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    resolution = PrerequisiteResolution(
        feature_flags={"reviewing": False},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status="install_failed",
                required_flags=["reviewing"],
                installer_id="go-install",
                message="install failed",
            )
        },
    )
    create_kwargs = {}

    def fake_prepare(raw_prerequisites, *, feature_flags, surface, choose_installer, progress_handler=None):
        return resolution

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return _FakePipeline()

    monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr("iac_code.pipeline.config.get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: None)
    monkeypatch.setattr(repl_module, "prepare_prerequisites", fake_prepare, raising=False)
    monkeypatch.setattr("iac_code.pipeline.create_pipeline", fake_create_pipeline)

    await repl._handle_pipeline_chat("deploy")

    assert create_kwargs["prerequisite_resolution"] == resolution.to_metadata()
    messages = [call.args[0] for call in repl.renderer.print_system_message.call_args_list]
    assert any(
        "review" in message and "disabled" in message.lower() and "install failed" in message for message in messages
    )
    assert any("review step skipped" in message for message in messages)
    assert repl.console.print.call_count >= 1


@pytest.mark.asyncio
async def test_handle_pipeline_chat_ignores_stale_terminal_sidecar_prerequisites(tmp_path, monkeypatch):
    from iac_code.ui import repl as repl_module

    repl = _make_repl_for_pipeline_chat(tmp_path)
    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    sidecar = tmp_path / "session-1" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "status": "discarded",
                "updated_at": 0.0,
                "prerequisites": {"feature_flags": {"reviewing": False}, "decisions": {}, "env_overrides": {}},
            }
        ),
        encoding="utf-8",
    )
    resolution = PrerequisiteResolution(
        feature_flags={"reviewing": True},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status="available",
                required_flags=["reviewing"],
                resolved_path="/usr/local/bin/infraguard",
            )
        },
    )
    prepare_calls = []
    create_kwargs = {}

    def fake_prepare(raw_prerequisites, *, feature_flags, surface, choose_installer, progress_handler=None):
        prepare_calls.append((raw_prerequisites, feature_flags, surface, choose_installer))
        return resolution

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return _FakePipeline()

    monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr("iac_code.pipeline.config.get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: None)
    monkeypatch.setattr(repl_module, "prepare_prerequisites", fake_prepare, raising=False)
    monkeypatch.setattr("iac_code.pipeline.create_pipeline", fake_create_pipeline)

    await repl._handle_pipeline_chat("deploy")

    assert len(prepare_calls) == 1
    assert create_kwargs["prerequisite_resolution"] == resolution.to_metadata()
    repl._detect_pipeline_session.assert_called_once_with(str(tmp_path), "session-1")


@pytest.mark.asyncio
async def test_swap_session_clears_pipeline_reference(tmp_path):
    """问题 4-a：swap 后 self._pipeline 必须 None（防 sidecar 污染）。"""
    from iac_code.ui.repl import InlineREPL

    repl, _ = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    # 目标 session 无 sidecar
    await repl.swap_session_async("new")
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_swap_session_no_sidecar_enters_normal_mode(tmp_path):
    """问题 4-b：目标 session 无 sidecar → 不弹确认，普通模式。"""
    from iac_code.ui.repl import InlineREPL

    repl, _ = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl._runtime_mode = RunMode.PIPELINE
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock()  # 不应被调

    await repl.swap_session_async("new")
    repl._confirm_pipeline_resume.assert_not_called()
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL


@pytest.mark.asyncio
async def test_swap_session_detects_target_sidecar_and_prompts(tmp_path):
    """问题 4-c：目标 session 有 sidecar → 弹确认 UI。"""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    # 在目标 session 下放 sidecar
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    await repl.swap_session_async("new")
    repl._confirm_pipeline_resume.assert_called_once()
    # discard 选择 → 不创建新 pipeline, but keep the sidecar for debugging.
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL
    assert sidecar.exists()
    meta = yaml.safe_load((sidecar / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["status"] == "discarded"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["reason"] == "discarded from /resume picker"


@pytest.mark.asyncio
async def test_swap_session_discard_marks_sidecar_without_deleting(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    with (
        patch("iac_code.pipeline.engine.session.PipelineSession.delete") as delete,
        patch("iac_code.pipeline.engine.session.PipelineSession.mark_discarded") as mark_discarded,
    ):
        await repl.swap_session_async("new")

    delete.assert_not_called()
    mark_discarded.assert_called_once_with(reason="discarded from /resume picker")


@pytest.mark.asyncio
async def test_swap_session_discard_mark_failure_does_not_crash_or_delete(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    with (
        patch("iac_code.pipeline.engine.session.PipelineSession.delete") as delete,
        patch(
            "iac_code.pipeline.engine.session.PipelineSession.mark_discarded",
            side_effect=OSError("disk unavailable"),
        ) as mark_discarded,
    ):
        await repl.swap_session_async("new")

    delete.assert_not_called()
    mark_discarded.assert_called_once_with(reason="discarded from /resume picker")
    assert sidecar.exists()
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL
    repl.renderer.print_system_message.assert_called_once()
    assert repl.renderer.print_system_message.call_args.kwargs["style"] == "yellow"
    assert "disk unavailable" in repl.renderer.print_system_message.call_args.args[0]


@pytest.mark.asyncio
async def test_swap_session_discarded_sidecar_does_not_prompt(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "discarded", "current_step": None, "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock()

    await repl.swap_session_async("new")

    repl._confirm_pipeline_resume.assert_not_called()
    assert repl._pipeline is None


@pytest.mark.asyncio
async def test_swap_session_resume_choice_creates_pipeline(tmp_path, monkeypatch):
    """问题 4-d：用户选 resume → 重建 self._pipeline。"""
    import yaml

    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    stored_prerequisites = {
        "feature_flags": {"reviewing": False},
        "decisions": {},
        "env_overrides": {"INFRAGUARD_PATH": "/tmp/resumed/infraguard"},
    }
    (sidecar / "meta.yaml").write_text(
        yaml.dump(
            {
                "status": "running",
                "current_step": "step1",
                "state_machine": {},
                "updated_at": 0.0,
                "prerequisites": stored_prerequisites,
            }
        ),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = ["mocked_skill"]
    monkeypatch.delenv("INFRAGUARD_PATH", raising=False)

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with (
        patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline) as cp,
        patch.object(
            repl_module,
            "prepare_prerequisites",
            side_effect=AssertionError("stored sidecar prerequisites should not be prepared again"),
        ),
    ):
        await repl.swap_session_async("new")

    cp.assert_called_once()
    assert cp.call_args.kwargs["resume_from_sidecar"] is True
    assert cp.call_args.kwargs["session_id"] == "new"
    assert cp.call_args.kwargs["prerequisite_resolution"] == stored_prerequisites
    assert "INFRAGUARD_PATH" not in os.environ
    # Regression: /resume path must forward auto_trigger_skills so model-invocable
    # skills survive a session swap mid-pipeline.
    assert cp.call_args.kwargs.get("auto_trigger_skills") == ["mocked_skill"]
    assert repl._pipeline is fake_pipeline


@pytest.mark.asyncio
async def test_swap_session_resume_choice_empty_sidecar_prerequisites_skip_prepare(tmp_path):
    import yaml

    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump(
            {
                "status": "running",
                "current_step": "step1",
                "state_machine": {},
                "updated_at": 0.0,
                "prerequisites": {},
            }
        ),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with (
        patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline) as cp,
        patch.object(
            repl_module,
            "prepare_prerequisites",
            side_effect=AssertionError("empty sidecar prerequisites should still win"),
        ),
    ):
        await repl.swap_session_async("new")

    assert cp.call_args.kwargs["resume_from_sidecar"] is True
    assert cp.call_args.kwargs["prerequisite_resolution"] == {}


@pytest.mark.asyncio
async def test_swap_session_resume_choice_switches_runtime_mode_to_pipeline(tmp_path):
    """A resumed sidecar must route subsequent chat turns back to pipeline mode."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl._runtime_mode = RunMode.NORMAL
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    assert repl._runtime_mode == RunMode.PIPELINE
    assert repl._pipeline is fake_pipeline


@pytest.mark.asyncio
async def test_ensure_pipeline_restored_for_prompt_uses_sidecar_prerequisites(tmp_path):
    import yaml

    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl._pipeline = None
    repl._runtime_mode = RunMode.PIPELINE
    repl._get_runtime_mode = InlineREPL._get_runtime_mode.__get__(repl)
    repl.ensure_pipeline_restored_for_prompt = InlineREPL.ensure_pipeline_restored_for_prompt.__get__(repl)
    repl._detect_pipeline_session = MagicMock(return_value=True)
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl._pipeline_memory_content_getter = MagicMock(return_value=lambda: "")
    repl._refresh_pipeline_display_recorder = MagicMock()
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    stored_prerequisites = {"feature_flags": {"reviewing": False}, "decisions": {}, "env_overrides": {}}
    sidecar = sessions_root / "old" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump(
            {
                "status": "running",
                "current_step": "step1",
                "state_machine": {},
                "updated_at": 0.0,
                "prerequisites": stored_prerequisites,
            }
        ),
        encoding="utf-8",
    )

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with (
        patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline) as cp,
        patch.object(
            repl_module,
            "prepare_prerequisites",
            side_effect=AssertionError("stored sidecar prerequisites should not be prepared again"),
        ),
    ):
        restored = await repl.ensure_pipeline_restored_for_prompt()

    assert restored is True
    assert cp.call_args.kwargs["resume_from_sidecar"] is True
    assert cp.call_args.kwargs["prerequisite_resolution"] == stored_prerequisites
    assert repl._pipeline is fake_pipeline


@pytest.mark.asyncio
async def test_swap_session_running_resume_routes_next_message_to_interrupt_judge(tmp_path):
    """A running sidecar restored via /resume must judge the next input."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._render_pipeline_stream = AsyncMock(return_value=None)
    repl._handoff_pipeline_to_normal = MagicMock(return_value=None)

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    fake_pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    fake_pipeline.resume = MagicMock(return_value=_empty_stream())
    fake_pipeline.sidecar_status = "running"
    fake_pipeline.state_machine.is_complete = False
    fake_pipeline.mark_user_aborted = MagicMock()
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    await repl._handle_pipeline_chat("change the plan")

    fake_pipeline.continue_from_sidecar.assert_called_once_with(
        user_input=PipelineUserInput(content="change the plan", display_text="change the plan", has_images=False)
    )
    fake_pipeline.resume.assert_not_called()


@pytest.mark.asyncio
async def test_swap_session_waiting_input_resume_routes_next_message_to_resume(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "waiting_input", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._render_pipeline_stream = AsyncMock(return_value=None)
    repl._handoff_pipeline_to_normal = MagicMock(return_value=None)

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="waiting_input", reason=None)
    fake_pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    fake_pipeline.resume = MagicMock(return_value=_empty_stream())
    fake_pipeline.sidecar_status = "waiting_input"
    fake_pipeline.state_machine.is_complete = False
    fake_pipeline.mark_user_aborted = MagicMock()
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    await repl._handle_pipeline_chat("option A")

    fake_pipeline.resume.assert_called_once_with(
        PipelineUserInput(content="option A", display_text="option A", has_images=False)
    )
    fake_pipeline.continue_from_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_swap_session_resume_failed_restore_keeps_pipeline_none(tmp_path):
    """If PipelineRunner construction could not restore, /resume must not claim success."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(
        ok=False,
        status="running",
        reason="pipeline_identity_mismatch",
    )
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    assert repl._pipeline is None
    repl.renderer.print_system_message.assert_called()


async def _empty_stream():
    return
    yield  # noqa: B901


def test_refresh_cloud_tools_reuses_repl_aliyun_services(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    repl = object.__new__(InlineREPL)
    repl.tool_registry = MagicMock()
    repl._aliyun_services = object()
    register = MagicMock()
    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", register)

    InlineREPL.refresh_cloud_tools(repl)

    assert register.call_args.args[0] is repl.tool_registry
    assert register.call_args.args[2] is repl._aliyun_services


@pytest.mark.asyncio
async def test_close_aliyun_services_closes_shared_runtime() -> None:
    from iac_code.ui.repl import InlineREPL

    repl = object.__new__(InlineREPL)
    services = SimpleNamespace(aclose=AsyncMock())
    repl._aliyun_services = services

    await InlineREPL._close_aliyun_services(repl)

    services.aclose.assert_awaited_once()
