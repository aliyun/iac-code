from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "e2e" / "run_recovery_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_recovery_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _input_required_event(kind: str) -> dict:
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "input_required",
                            "data": {"kind": kind},
                        }
                    }
                }
            }
        }
    }


def test_latest_input_required_kind_from_events_uses_latest_kind() -> None:
    runner = _load_runner()

    kind = runner._latest_input_required_kind_from_events(
        [
            _input_required_event("ask_user_question"),
            _input_required_event("candidate_selection"),
        ]
    )

    assert kind == "candidate_selection"


def test_default_recovery_prompt_targets_previous_real_user_question() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_RECOVERY_PROMPT
    assert "[Pipeline Handoff Context]" in runner.DEFAULT_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_RECOVERY_PROMPT


def test_normal_running_recovery_prompt_ignores_continue() -> None:
    runner = _load_runner()

    assert "我刚才问了你哪些问题" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "最后一条真实用户消息原文" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "内容等于“继续”" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "请完成当前步骤" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
    assert "更早的方案选择消息" in runner.DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT


def test_text_image_fixture_store_writes_png_and_manifest(tmp_path: Path) -> None:
    runner = _load_runner()
    store = runner.TextImageFixtureStore(tmp_path / "image-fixtures")

    part = store.part("runtime-only", runner.DEFAULT_INITIAL_PROMPT)

    assert part["filename"] == "runtime-only.png"
    assert part["mediaType"] == "image/png"
    assert base64.b64decode(part["bytes"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert (tmp_path / "image-fixtures" / "runtime-only.png").is_file()
    manifest = json.loads((tmp_path / "image-fixtures" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime-only"]["text"] == runner.DEFAULT_INITIAL_PROMPT
    assert manifest["runtime-only"]["mediaType"] == "image/png"
    assert manifest["runtime-only"]["source"] == "generated"


def test_static_text_image_fixtures_cover_fixed_image_prompts() -> None:
    runner = _load_runner()
    manifest = json.loads((runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest) == set(runner.STATIC_TEXT_IMAGE_FIXTURES)
    for key, text in runner.STATIC_TEXT_IMAGE_FIXTURES.items():
        entry = manifest[key]
        fixture_path = runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / entry["filename"]
        assert entry["text"] == text
        assert entry["mediaType"] == "image/png"
        assert fixture_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_text_image_fixture_store_prefers_static_fixture(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    store = runner.TextImageFixtureStore(tmp_path / "image-fixtures")
    static_manifest = json.loads((runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    def fail_render(_text: str) -> bytes:
        raise AssertionError("static fixtures should avoid runtime image rendering")

    monkeypatch.setattr(runner, "_render_text_png", fail_render)

    part = store.part("initial", runner.STATIC_TEXT_IMAGE_FIXTURES["initial"])

    static_path = runner.STATIC_TEXT_IMAGE_FIXTURE_ROOT / static_manifest["initial"]["filename"]
    assert part["filename"] == static_path.name
    assert part["mediaType"] == "image/png"
    assert base64.b64decode(part["bytes"]) == static_path.read_bytes()
    manifest = json.loads((tmp_path / "image-fixtures" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["initial"]["source"] == "static"
    assert manifest["initial"]["path"] == str(static_path)


def test_scenario_harness_stream_passes_image_parts(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    captured: dict[str, object] = {}

    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        cwd="",
        port=0,
        host="127.0.0.1",
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
        stream_timeout=1,
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path / "runs"),
        python=sys.executable,
        leave_server_running=False,
    )
    harness = runner.ScenarioHarness(args, scenario="image-initial")
    image = {"filename": "initial.png", "mediaType": "image/png", "bytes": "iVBORw0KGgo="}

    def fake_stream_message(**kwargs):
        captured.update(kwargs)
        return runner.StreamSummary(
            name=kwargs["name"],
            prompt=kwargs["prompt"],
            request_task_id=kwargs["task_id"],
            task_id="task-1",
            context_id="ctx-1",
        )

    monkeypatch.setattr(runner, "stream_message", fake_stream_message)

    harness.stream(prompt=runner.IMAGE_TEXT_PROMPT, name="01-image", context_id="", task_id="", images=[image])

    assert captured["images"] == [image]


def test_image_recovery_scenarios_are_registered() -> None:
    runner = _load_runner()

    for scenario in [
        "image-initial",
        "image-ask-waiting",
        "image-selection-waiting",
        "image-normal-handoff",
        "image-interrupt",
    ]:
        assert scenario in runner._SCENARIOS
        assert scenario in runner._REAL_CLOUD_SCENARIOS


def test_answer_intervening_ask_inputs_reaches_selection(tmp_path: Path) -> None:
    runner = _load_runner()
    initial = runner.StreamSummary(
        name="01-initial",
        prompt="选择一个已有vpc，创建一个vswitch",
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="intent_parsing",
    )
    (tmp_path / "01-initial.events.jsonl").write_text(
        json.dumps(_input_required_event("ask_user_question"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    selection = runner.StreamSummary(
        name="01-initial-answer-ask-1",
        prompt=runner.INTERVENING_ASK_ANSWER,
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="confirm_and_select",
    )
    prompts: list[str] = []

    def stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "01-initial-answer-ask-1"
        return selection

    harness = SimpleNamespace(run_dir=tmp_path, notes=[], stream=stream)

    result = runner._answer_intervening_ask_inputs(harness, initial, name_prefix="01-initial")

    assert result is selection
    assert prompts == [runner.INTERVENING_ASK_ANSWER]
    assert result.last_input_required_step_id == "confirm_and_select"


def test_hydrated_task_checks_require_omitted_request_task_id() -> None:
    runner = _load_runner()
    harness = SimpleNamespace(checks={}, context_id="ctx-1", pipeline_task_id="task-1")
    summary = runner.StreamSummary(
        name="resume",
        prompt="继续",
        request_task_id="",
        context_id="ctx-1",
        task_id="task-1",
    )

    runner._add_hydrated_task_checks(harness, summary, "resume")

    assert harness.checks == {
        "resume omitted taskId": True,
        "resume stayed in recovered context": True,
        "resume hydrated recovered taskId": True,
    }


def test_fault_after_snapshot_continuation_uses_context_only_hydration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    task = {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}
    task_list_response = {"response": {"result": {"tasks": [task]}}}
    task_get_response = {"response": {"result": task}}
    fake_harnesses = []

    class FakeBackgroundStream:
        def __init__(
            self,
            *,
            prompt: str,
            context_id: str,
            task_id: str,
            name: str,
            **_kwargs,
        ) -> None:
            self.name = name
            self.summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=task_id,
                context_id=context_id,
            )

        def start(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            pass

    class FakeHarness:
        def __init__(self, args) -> None:
            self.args = args
            self.server_url = "http://127.0.0.1:1"
            self.cwd = str(tmp_path)
            self.run_dir = tmp_path
            self.server_env = {}
            self.summaries = {}
            self.snapshots = {}
            self.checks = {}
            self.notes = []
            self.context_id = ""
            self.pipeline_task_id = ""
            self.stream_request_task_ids = []

        def wait_for_server_exit(self, *, expected_returncode: int, timeout: float) -> int:
            return expected_returncode

        def disable_fault_injection(self) -> None:
            pass

        def start_server(self) -> None:
            pass

        def fetch_state(self, name: str):
            return {"snapshot": {"status": "working"}}

        def capture_task_snapshots(self, name: str):
            return {"task_get": task_get_response, "task_list": task_list_response}

        def stream(self, *, prompt: str, name: str, task_id: str | None = None):
            request_task_id = self.pipeline_task_id if task_id is None else task_id
            self.stream_request_task_ids.append(request_task_id)
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=request_task_id,
                context_id=self.context_id,
                task_id=self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                text="created vsw-123",
            )
            self.summaries[name] = summary
            return summary

    def fake_run_with_harness(args, scenario, callback):
        harness = FakeHarness(args)
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "BackgroundStream", FakeBackgroundStream)
    monkeypatch.setattr(runner, "fetch_tasks", lambda **_kwargs: task_list_response)
    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)

    args = SimpleNamespace(
        deterministic=True,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        stream_timeout=1,
        event_timeout=1,
    )

    assert runner.run_fault_after_snapshot(args, "fault-after-snapshot") == 0
    assert fake_harnesses[0].stream_request_task_ids == [""]
    assert fake_harnesses[0].checks["continue omitted taskId"] is True
    assert fake_harnesses[0].checks["continue hydrated recovered taskId"] is True


def test_fault_after_snapshot_requires_real_cloud_opt_in_even_when_deterministic() -> None:
    runner = _load_runner()
    args = SimpleNamespace(deterministic=True, allow_real_cloud=False)

    try:
        runner._validate_scenario_execution(args, "fault-after-snapshot")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("fault-after-snapshot should require --allow-real-cloud")


def test_fault_after_snapshot_allows_explicit_real_cloud_opt_in() -> None:
    runner = _load_runner()
    args = SimpleNamespace(deterministic=True, allow_real_cloud=True)

    runner._validate_scenario_execution(args, "fault-after-snapshot")


def test_rollback_step5_cleanup_scenarios_are_registered_and_require_real_cloud() -> None:
    runner = _load_runner()

    assert runner._SCENARIOS["rollback-step5-cleanup"] is runner.run_rollback_step5_cleanup
    assert runner._SCENARIOS["rollback-step5-cleanup-recovery"] is runner.run_rollback_step5_cleanup_recovery

    for scenario in ("rollback-step5-cleanup", "rollback-step5-cleanup-recovery"):
        args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
        try:
            runner._validate_scenario_execution(args, scenario)
        except SystemExit as exc:
            assert "--allow-real-cloud" in str(exc)
        else:
            raise AssertionError(f"{scenario} should require --allow-real-cloud")


def test_stack_cleanup_snapshot_helpers_distinguish_deleted_and_retained_stacks() -> None:
    runner = _load_runner()
    snapshot = {
        "snapshot": {
            "cleanup": {
                "resources": [
                    {
                        "provider": "ros",
                        "resourceType": "stack",
                        "resourceId": "stack-1",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "completed",
                        "stackStatus": "DELETE_COMPLETE",
                    }
                ]
            },
            "stacks": {
                "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                "byId": {
                    "stack-1": {"stackId": "stack-1", "current": False, "cleared": True},
                    "stack-2": {"stackId": "stack-2", "current": True},
                    "stack-3": {"stackId": "stack-3", "isSuccess": False, "stackStatus": "CREATE_FAILED"},
                },
            },
        }
    }

    cleanup_resource = runner._cleanup_resource_for_stack(snapshot, "stack-1")
    assert cleanup_resource["cleanupStatus"] == "completed"
    assert runner._cleanup_resource_completed(cleanup_resource) is True
    assert runner._cleanup_resource_completed({"cleanupStatus": "completed"}) is False
    assert runner._snapshot_current_stack_id(snapshot, exclude={"stack-1"}) == "stack-2"
    assert runner._snapshot_current_stack_id(snapshot, exclude={"stack-2"}) is None
    assert runner._ros_stack_deleted({"status": "DELETE_COMPLETE"}) is True
    assert runner._ros_stack_deleted({"not_found": True}) is True
    assert runner._ros_stack_retained({"status": "CREATE_COMPLETE"}) is True
    assert runner._ros_stack_retained({"status": "DELETE_COMPLETE"}) is False
    assert runner._ros_stack_retained({"status": "DELETE_ROLLBACK_COMPLETE"}) is False


def _stack_current_changed_event(
    *,
    action: str,
    stack_id: str,
    status: str,
    is_success: bool,
    cleared: bool = False,
) -> dict:
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "stack_current_changed",
                            "data": {
                                "provider": "ros",
                                "action": action,
                                "stackId": stack_id,
                                "stackStatus": status,
                                "isSuccess": is_success,
                                "cleared": cleared,
                            },
                        }
                    }
                }
            }
        }
    }


def test_wait_for_created_stack_uses_successful_stack_event() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="failed-stack",
            status="CREATE_FAILED",
            is_success=False,
        ),
        _stack_current_changed_event(
            action="DeleteStack",
            stack_id="failed-stack",
            status="DELETE_COMPLETE",
            is_success=True,
            cleared=True,
        ),
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert runner._wait_for_created_stack(FakeStream(), exclude=set(), timeout=1) == "created-stack"


def test_created_stack_id_from_stream_uses_only_that_stream_successes() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="failed-stack",
                status="CREATE_FAILED",
                is_success=False,
            ),
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="rollback-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="second-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude={"rollback-stack"}) == "second-stack"


def test_post_rollback_timeout_allows_step_regeneration_time() -> None:
    runner = _load_runner()

    args = SimpleNamespace(event_timeout=300, stream_timeout=2400)

    assert runner._post_rollback_timeout(args) == 900


def test_wait_any_ignores_finished_stream_when_another_stream_matches() -> None:
    runner = _load_runner()
    match = runner.EventMatch(
        description="target",
        event={"ok": True},
        summary=runner.StreamSummary(name="active", prompt=""),
    )

    class FinishedStream:
        name = "finished"

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("finished ended before target")

    class ActiveStream:
        name = "active"

        def wait_for(self, *_args, **_kwargs):
            return match

    assert (
        runner._wait_any([FinishedStream(), ActiveStream()], lambda *_args: True, description="target", timeout=1)
        is match
    )


def test_cleanup_ledger_items_use_a2a_context_session_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = _load_runner()

    cwd = str((tmp_path / "workspace").resolve())
    Path(cwd).mkdir()
    run_dir = tmp_path / "run"
    contexts_dir = run_dir / "a2a-persistence" / "contexts"
    contexts_dir.mkdir(parents=True)
    (contexts_dir / "ctx-1.json").write_text(
        json.dumps({"context_id": "ctx-1", "session_id": "session-1", "cwd": cwd}),
        encoding="utf-8",
    )

    from iac_code.services.session_storage import SessionStorage

    ledger_dir = SessionStorage().session_dir(cwd, "session-1") / "pipeline"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "cleanup.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                "observed_resources:",
                "- provider: ros",
                "  resource_type: stack",
                "  resource_id: stack-1",
                "  observed_action: CreateStack",
                "cleanup_resources: []",
                "history: []",
            ]
        ),
        encoding="utf-8",
    )

    harness = SimpleNamespace(context_id="ctx-1", cwd=cwd, run_dir=run_dir)

    items = runner._cleanup_ledger_items(harness, "observed_resources")

    assert [item["resource_id"] for item in items] == ["stack-1"]


def test_cleanup_activity_snapshot_helper_ignores_empty_default_cleanup() -> None:
    runner = _load_runner()

    assert (
        runner._snapshot_has_cleanup_activity(
            {"snapshot": {"cleanup": {"status": "none", "resourceCount": 0, "resources": [], "history": []}}}
        )
        is False
    )
    assert runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"resourceCount": "1"}}}) is True
    assert runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"status": "pending"}}}) is True
    assert (
        runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"resources": [{"resourceId": "stack-1"}]}}})
        is True
    )
    assert runner._snapshot_has_cleanup_activity({"snapshot": {"cleanup": {"history": [{"eventType": "x"}]}}}) is True


def test_cleanup_activity_event_helper_detects_cleanup_events_and_handoff_data(tmp_path: Path) -> None:
    runner = _load_runner()
    normal_path = tmp_path / "normal.events.jsonl"
    cleanup_path = tmp_path / "cleanup.events.jsonl"
    handoff_path = tmp_path / "handoff.events.jsonl"
    normal_path.write_text(
        json.dumps(
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="stack-1",
                status="CREATE_COMPLETE",
                is_success=True,
            )
        ),
        encoding="utf-8",
    )
    cleanup_path.write_text(
        json.dumps(
            {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "cleanup_started",
                                    "scope": "cleanup",
                                    "data": {"resourceId": "stack-1"},
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    handoff_path.write_text(
        json.dumps(
            {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "pipeline_handoff_ready",
                                    "data": {"cleanup": {"resourceCount": 1}},
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert runner._events_file_has_cleanup_activity(normal_path) is False
    assert runner._events_file_has_cleanup_activity(cleanup_path) is True
    assert runner._events_file_has_cleanup_activity(handoff_path) is True
    assert runner._run_dir_has_cleanup_events(tmp_path) is True


def test_session_file_has_cleanup_prompt_uses_metadata_type(tmp_path: Path) -> None:
    runner = _load_runner()
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "visible"}),
                json.dumps(
                    {
                        "role": "user",
                        "content": "hidden cleanup prompt",
                        "metadata": {"type": "pipeline_cleanup_prompt"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert runner._session_file_has_cleanup_prompt(session_path) is True


def test_cleanup_ledger_required_resources_helper_ignores_observed_only() -> None:
    runner = _load_runner()
    harness = SimpleNamespace()

    assert runner._cleanup_ledger_has_required_resources(harness) is False

    original = runner._cleanup_ledger_items
    try:
        runner._cleanup_ledger_items = lambda _h, key: (
            [{"resource_id": "stack-1", "cleanup_required": False}]
            if key == "cleanup_resources"
            else [{"resource_id": "stack-observed"}]
        )
        assert runner._cleanup_ledger_has_required_resources(harness) is False
        runner._cleanup_ledger_items = lambda _h, key: (
            [{"resource_id": "stack-2", "cleanup_required": True}] if key == "cleanup_resources" else []
        )
        assert runner._cleanup_ledger_has_required_resources(harness) is True
    finally:
        runner._cleanup_ledger_items = original


def test_cleanup_deployment_prompts_use_distinct_run_scoped_stack_names(tmp_path: Path) -> None:
    runner = _load_runner()
    harness = SimpleNamespace(run_dir=tmp_path / "20260617T010203Z-12345-abcdef12")

    first = runner._cleanup_deployment_prompt("你随便选一个方案。", harness, "first")
    second = runner._cleanup_deployment_prompt("你随便选一个方案。", harness, "second")

    assert "唯一成功条件是新建一个 ROS stack" in first
    assert "任何已有 stack" in first
    assert "不能作为部署成功依据" in first
    assert "StackName" in first
    assert "必须覆盖为 `iac-e2e-abcdef12-first`" in first
    assert "不要调用 complete_step" in first
    assert "等待用户下一条指令" in first
    assert "iac-e2e-abcdef12-first" in first
    assert "iac-e2e-abcdef12-second" in second
    assert "complete_step 前必须" in second
    assert first != second


def test_rollback_step5_cleanup_flow_cleans_first_stack_and_keeps_second(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []
            self.started_streams: list[str] = []

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.started_streams.append(name)
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.notes.append("restarted")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup(args, "rollback-step5-cleanup") == 0
    harness = fake_harnesses[0]
    assert harness.stream_calls[-1]["task_id"] == ""
    assert harness.checks["first rollback stack cleanup completed in snapshot"] is True
    assert harness.checks["rollback cleanup stacks completed in snapshot"] is True
    assert harness.checks["ROS first rollback stack deleted"] is True
    assert harness.checks["ROS rollback cleanup stacks deleted"] is True
    assert harness.checks["ROS second stack retained"] is True


def test_rollback_step5_cleanup_recovery_uses_tool_safe_recovery_prompt(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.notes.append("restarted")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_wait_for_cleanup_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_join_after_kill", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_events_file_has_cleanup_event",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup_recovery(args, "rollback-step5-cleanup-recovery") == 0
    recovery_prompt = next(
        call["prompt"] for call in fake_harnesses[0].stream_calls if call["name"] == "06-cleanup-after-restart"
    )
    assert recovery_prompt != runner.CONTINUE_PROMPT
    assert "不要调用任何工具" in recovery_prompt
    assert "不要查询" in recovery_prompt
    assert "不要删除" in recovery_prompt


def test_rollback_step5_cleanup_flow_fails_when_any_cleanup_stack_is_left(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "pending",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            },
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-left",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "pending",
                                "stackStatus": "CREATE_COMPLETE",
                            },
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            raise AssertionError("non-recovery scenario should not restart")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        },
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-left",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        },
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-left": {"status": "CREATE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup(args, "rollback-step5-cleanup") == 1


def test_rollback_step5_cleanup_recovery_kills_and_retriggers_cleanup(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    fake_harnesses = []

    class FakeStream:
        def __init__(self, summary: object, events: list[dict] | None = None) -> None:
            self.summary = summary
            self.name = summary.name
            self.events = events or []

        def wait_for(self, *_args, **_kwargs):
            return None

        def join(self, timeout: float):
            return self.summary

    class FakeHarness:
        def __init__(self) -> None:
            self.args = SimpleNamespace(stream_timeout=1, event_timeout=1)
            self.run_dir = tmp_path
            self.server_env = {}
            self.cwd = str(tmp_path)
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.checks: dict[str, bool] = {}
            self.notes: list[str] = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_calls: list[dict] = []
            self.started_streams: list[dict] = []
            self.kill_count = 0

        def stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.stream_calls.append({"prompt": prompt, "name": name, "task_id": task_id})
            is_initial = name == "01-initial"
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"] if is_initial else ["TASK_STATE_COMPLETED"],
                pipeline_event_types=["input_required"] if is_initial else ["pipeline_completed"],
                last_input_required_step_id="confirm_and_select" if is_initial else "",
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            return summary

        def start_stream(self, *, prompt: str, name: str, task_id: str | None = None, **_kwargs):
            self.started_streams.append({"prompt": prompt, "name": name, "task_id": task_id})
            summary = runner.StreamSummary(
                name=name,
                prompt=prompt,
                request_task_id=self.pipeline_task_id if task_id is None else task_id,
                context_id=self.context_id,
                task_id="normal-task" if task_id == "" else self.pipeline_task_id,
                status_states=["TASK_STATE_COMPLETED"],
                pipeline_event_types=["pipeline_completed"],
                normal_handoff_ready=True,
                text="done",
            )
            self.summaries[name] = summary
            events = []
            if name == "04-select-second-stack":
                events.append(
                    _stack_current_changed_event(
                        action="CreateStack",
                        stack_id="stack-2",
                        status="CREATE_COMPLETE",
                        is_success=True,
                    )
                )
            return FakeStream(summary, events=events)

        def fetch_state(self, name: str):
            snapshot = {
                "snapshot": {
                    "status": "completed",
                    "cleanup": {
                        "status": "completed",
                        "resources": [
                            {
                                "provider": "ros",
                                "resourceType": "stack",
                                "resourceId": "stack-1",
                                "regionId": "cn-hangzhou",
                                "cleanupStatus": "completed",
                                "stackStatus": "DELETE_COMPLETE",
                            }
                        ],
                    },
                    "stacks": {
                        "current": {"stackId": "stack-2", "regionId": "cn-hangzhou", "current": True},
                        "byId": {"stack-2": {"stackId": "stack-2", "current": True}},
                    },
                }
            }
            self.snapshots[name] = snapshot
            return snapshot

        def kill9_and_restart(self) -> None:
            self.kill_count += 1

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        fake_harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    cleanup_ledger_items = [
        {
            "provider": "ros",
            "resource_type": "stack",
            "resource_id": "stack-1",
            "region_id": "cn-hangzhou",
            "cleanup_required": True,
        }
    ]

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_answer_intervening_ask_inputs", lambda _h, summary, **_kwargs: summary)
    monkeypatch.setattr(runner, "_wait_for_created_stack", lambda *_args, **_kwargs: "stack-1")
    monkeypatch.setattr(runner, "_wait_any", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_wait_for_cleanup_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_events_file_has_cleanup_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_ledger_items",
        lambda _h, key: cleanup_ledger_items if key == "cleanup_resources" else [],
    )
    monkeypatch.setattr(
        runner,
        "_capture_ros_stack_states",
        lambda _h, stack_ids, name: {
            "stack-1": {"status": "DELETE_COMPLETE"},
            "stack-2": {"status": "CREATE_COMPLETE"},
        },
    )

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    )

    assert runner.run_rollback_step5_cleanup_recovery(args, "rollback-step5-cleanup-recovery") == 0
    harness = fake_harnesses[0]
    assert harness.kill_count == 1
    assert harness.started_streams[-1] == {
        "prompt": runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
        "name": "05-cleanup-running",
        "task_id": "",
    }
    assert harness.stream_calls[-1] == {
        "prompt": runner.CLEANUP_RECOVERY_PROMPT,
        "name": "06-cleanup-after-restart",
        "task_id": "",
    }
    assert harness.checks["cleanup retriggered after restart"] is True


def test_rollback_accepts_security_group_deployment_from_handoff(monkeypatch) -> None:
    runner = _load_runner()
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        "{\n"
        '  "deployment": {\n'
        '    "status": "success",\n'
        '    "resources_created": ["ALIYUN::ECS::SecurityGroup"],\n'
        '    "outputs": {"SecurityGroupId": "sg-test"}\n'
        "  }\n"
        "}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [{"id": "deploying", "status": "completed", "runId": "step-deploying-1"}],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    class FakeHarness:
        def __init__(self) -> None:
            self.checks: dict[str, bool] = {}
            self.run_dir = Path("/tmp/fake")

        def start_stream(self, **_kwargs):
            return SimpleNamespace()

        def fetch_state(self, name: str):
            if name == "after-rollback-completion":
                return final_state
            return {"snapshot": {"taskId": "task-1"}}

        def kill9_and_restart(self) -> None:
            pass

        def stream(self, **_kwargs):
            return runner.StreamSummary(name="resume", prompt="继续")

    def fake_run_with_harness(_args, _scenario, callback):
        harness = FakeHarness()
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", lambda *args, **kwargs: [args[1][0]])
    monkeypatch.setattr(runner, "_wait_any", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_join_after_kill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_completed_snapshot_or_stream", lambda *args, **kwargs: True)

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
    )

    assert runner.run_rollback(args, "rollback-step1") == 0
