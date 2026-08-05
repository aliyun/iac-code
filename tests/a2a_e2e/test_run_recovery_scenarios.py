from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "e2e" / "run_recovery_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_recovery_scenarios", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _input_required_event(kind: str = "", *, step_id: str = "") -> dict:
    data = {}
    if kind:
        data["kind"] = kind
    if step_id:
        data["stepId"] = step_id
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "input_required",
                            "step": {"id": step_id} if step_id else {},
                            "data": data,
                        }
                    }
                }
            }
        }
    }


def _pipeline_batch(*envelopes: dict) -> dict:
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipelineBatch": {
                            "events": list(envelopes),
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


def test_waiting_for_followup_ask_distinguishes_candidate_selection(tmp_path: Path) -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(
        name="02-answer-first-ask",
        prompt=runner.ASK_FIRST_ANSWER,
        pipeline_event_types=["input_required"],
        last_input_required_step_id="confirm_and_select",
    )
    events_path = tmp_path / "02-answer-first-ask.events.jsonl"
    events_path.write_text(
        json.dumps(_input_required_event("candidate_selection", step_id="confirm_and_select")) + "\n",
        encoding="utf-8",
    )
    harness = SimpleNamespace(run_dir=tmp_path)

    assert runner._waiting_for_followup_ask(harness, summary) is False

    events_path.write_text(
        json.dumps(_input_required_event("ask_user_question", step_id="intent_parsing")) + "\n",
        encoding="utf-8",
    )
    assert runner._waiting_for_followup_ask(harness, summary) is True


def test_deployment_success_requires_latest_success_and_stack_id(tmp_path: Path) -> None:
    runner = _load_runner()
    events_path = tmp_path / "02-answer.events.jsonl"

    def completed(sequence: int, conclusion: dict) -> dict:
        return _pipeline_batch(
            {
                "eventType": "step_completed",
                "sequence": sequence,
                "step": {"id": "deploying"},
                "data": {"conclusion": conclusion},
            }
        )

    events_path.write_text(
        "\n".join(
            [
                json.dumps(completed(1, {"status": "success", "stack_id": "stack-1"})),
                json.dumps(completed(2, {"status": "failed", "resources_created": []})),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert runner._deployment_succeeded_with_stack_id(tmp_path) is False

    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(completed(3, {"status": "success", "outputs": {"StackId": "stack-2"}})) + "\n")
    assert runner._deployment_succeeded_with_stack_id(tmp_path) is True


def test_recovery_predicates_and_evidence_inspect_every_batched_event() -> None:
    runner = _load_runner()
    event = _pipeline_batch(
        {"eventType": "text_delta", "data": {"text": "before"}},
        {
            "eventType": "input_required",
            "step": {"id": "confirm_and_select"},
            "data": {"kind": "candidate_selection", "stepId": "confirm_and_select"},
        },
        {"eventType": "rollback_completed", "data": {}},
    )

    assert runner._input_required_step("confirm_and_select")(event, None) is True
    assert runner._event_type("rollback_completed")(event, None) is True
    assert runner._latest_input_required_kind_from_events([event]) == "candidate_selection"
    assert runner._latest_input_required_step_id_from_events([event]) == "confirm_and_select"


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


def test_a2a_session_contains_user_message_reads_persisted_context(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    cwd = str(tmp_path / "workspace")
    Path(cwd).mkdir()
    run_dir = tmp_path / "run"
    context_dir = run_dir / "a2a-persistence" / "contexts"
    context_dir.mkdir(parents=True)
    (context_dir / "ctx-1.json").write_text(
        json.dumps({"context_id": "ctx-1", "cwd": cwd, "session_id": "session-1"}),
        encoding="utf-8",
    )
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    storage.save(
        cwd,
        "session-1",
        [
            Message(role="user", content="[Pipeline Handoff Context]\n..."),
            Message(role="user", content="你刚才创建了什么"),
            Message(role="assistant", content="没有实际部署任何云资源。"),
        ],
    )
    monkeypatch.setattr(runner, "SessionStorage", lambda: storage)
    harness = SimpleNamespace(run_dir=run_dir, context_id="ctx-1", cwd=cwd, notes=[])

    assert runner._a2a_session_contains_user_message(harness, "你刚才创建了什么") is True
    assert runner._a2a_session_contains_user_message(harness, "不存在的问题") is False
    assert harness.notes == []


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
    assert harness.server_env["IAC_CODE_MODEL"] == "qwen3.8-max"
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


def test_default_models_are_selected_per_scenario() -> None:
    runner = _load_runner()
    args = runner.parse_args([])

    assert runner._model_for_scenario(args, "scenario1") == "deepseek-v4-flash-0731"
    assert runner._model_for_scenario(args, "image-initial") == "qwen3.8-max"


def test_explicit_model_overrides_every_scenario() -> None:
    runner = _load_runner()
    args = runner.parse_args(["--model", "custom-model"])

    assert runner._model_for_scenario(args, "scenario1") == "custom-model"
    assert runner._model_for_scenario(args, "image-initial") == "custom-model"


def test_scenario1_performance_backup_is_registered_and_requires_real_cloud() -> None:
    runner = _load_runner()

    assert runner._SCENARIOS["scenario1-performance-backup"] is runner.run_scenario1_performance_backup
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, "scenario1-performance-backup")
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("scenario1-performance-backup should require --allow-real-cloud")


def test_selection_during_backup_is_registered_and_requires_real_cloud() -> None:
    runner = _load_runner()

    scenario = runner.SELECTION_DURING_BACKUP_SCENARIO
    assert runner._SCENARIOS[scenario] is runner.run_selection_during_backup
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, scenario)
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError(f"{scenario} should require --allow-real-cloud")


def test_redaction_step4_is_registered_requires_real_cloud_and_forces_safe_mode(tmp_path: Path) -> None:
    runner = _load_runner()

    assert runner._SCENARIOS[runner.REDACTION_STEP4_SCENARIO] is runner.run_redaction_step4
    args = SimpleNamespace(allow_real_cloud=False, deterministic=False)
    try:
        runner._validate_scenario_execution(args, runner.REDACTION_STEP4_SCENARIO)
    except SystemExit as exc:
        assert "--allow-real-cloud" in str(exc)
    else:
        raise AssertionError("redaction-step4 should require --allow-real-cloud")

    harness_args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )
    harness = runner.ScenarioHarness(harness_args, scenario=runner.REDACTION_STEP4_SCENARIO)

    assert harness.server_env["IAC_CODE_A2A_SAFE_MODE"] == "true"


def test_step4_redaction_audit_preserves_credentials_tokens_and_only_hides_paths() -> None:
    runner = _load_runner()
    server_root = "/srv/iac-code-e2e"
    canonical = {
        "status": "waiting_input",
        "pendingInput": {
            "step": {"id": "confirm_and_select"},
            "options": [{"name": "economy"}, {"name": "balanced"}],
        },
        "steps": [
            {
                "conclusion": {
                    "deployment_parameters": {"RdsMasterUserPassword": "real-generated-value"},
                    "preview_validation": {"parameters": {"RdsMasterUserPassword": "real-generated-value"}},
                    "template_url": f"{server_root}/workspace/backend.yml",
                }
            }
        ],
        "display": {"usage": {"totalTokens": 1234}},
        "failedToolCall": {
            "path": f"{server_root}-rerun/src/iac_code/a2a/app.py",
            "result": f"File not found: {server_root}-rerun/src/iac_code/a2a/app.py",
        },
    }
    public = json.loads(json.dumps(canonical))
    public["steps"][0]["conclusion"]["template_url"] = "[PATH]"

    audit = runner._build_step4_redaction_audit(
        canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )

    assert all(runner._step4_redaction_checks(audit).values())
    assert "real-generated-value" not in json.dumps(audit)
    assert audit["canonicalKnownServerPathOccurrences"] == 1
    assert audit["publicKnownServerPathOccurrences"] == 0

    leaked_public = json.loads(json.dumps(public))
    leaked_public["steps"][0]["conclusion"]["template_url"] = f"{server_root}/workspace/backend.yml"
    leaked_audit = runner._build_step4_redaction_audit(
        canonical,
        leaked_public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )

    assert leaked_audit["publicKnownServerPathOccurrences"] == 1
    assert runner._step4_redaction_checks(leaked_audit)["safe mode hides known server paths from public state"] is False

    public["steps"][0]["conclusion"]["deployment_parameters"]["RdsMasterUserPassword"] = "***"
    broken_audit = runner._build_step4_redaction_audit(
        canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )
    broken_checks = runner._step4_redaction_checks(broken_audit)

    assert broken_checks["public functional parameters contain no redaction placeholders"] is False
    assert broken_checks["public credential fields match canonical values"] is False

    broken_canonical = json.loads(json.dumps(canonical))
    broken_canonical["steps"][0]["conclusion"]["deployment_parameters"]["RdsMasterUserPassword"] = "***"
    broken_canonical["display"]["usage"]["totalTokens"] = "***"
    canonical_audit = runner._build_step4_redaction_audit(
        broken_canonical,
        public,
        known_server_paths=(server_root,),
        safe_mode="true",
    )
    canonical_checks = runner._step4_redaction_checks(canonical_audit)

    assert canonical_checks["canonical functional parameters contain no redaction placeholders"] is False
    assert canonical_checks["canonical token counters are numeric when present"] is False


def test_redaction_step4_stops_before_selection_and_writes_only_audit(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    prompts: list[str] = []
    server_root = str(tmp_path / "server")
    canonical = {
        "status": "waiting_input",
        "pendingInput": {
            "step": {"id": "confirm_and_select"},
            "options": [{"name": "economy"}, {"name": "balanced"}],
        },
        "steps": [
            {
                "conclusion": {
                    "deployment_parameters": {"RdsMasterUserPassword": "real-generated-value"},
                    "template_url": f"{server_root}/backend.yml",
                }
            }
        ],
        "display": {"usage": {"totalTokens": 1234}},
    }
    public = {"snapshot": json.loads(json.dumps(canonical))}
    public["snapshot"]["steps"][0]["conclusion"]["template_url"] = "[PATH]"

    class FakeHarness:
        def __init__(self) -> None:
            self.context_id = "ctx-1"
            self.pipeline_task_id = "task-1"
            self.cwd = server_root
            self.server_cwd = server_root
            self.server_url = "http://127.0.0.1:1"
            self.run_dir = tmp_path
            self.server_env = {"IAC_CODE_A2A_SAFE_MODE": "true"}
            self.checks = {}
            self.snapshots = {}
            self.notes = []

        def stream(self, *, prompt: str, name: str, context_id: str, task_id: str):
            prompts.append(prompt)
            assert name == "01-redaction-step4"
            assert context_id == ""
            assert task_id == ""
            return runner.StreamSummary(
                name=name,
                prompt=prompt,
                task_id=self.pipeline_task_id,
                context_id=self.context_id,
                status_states=["TASK_STATE_INPUT_REQUIRED"],
                pipeline_event_types=["input_required"],
                last_input_required_step_id="confirm_and_select",
            )

    harness = FakeHarness()

    def fake_run_with_harness(_args, _scenario, callback):
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_load_canonical_pipeline_snapshot", lambda _h: canonical)
    monkeypatch.setattr(runner, "_fetch_pipeline_state_for_redaction_audit", lambda _h: public)
    args = SimpleNamespace(redaction_step4_prompt=runner.REDACTION_STEP4_PROMPT)

    assert runner.run_redaction_step4(args, runner.REDACTION_STEP4_SCENARIO) == 0
    assert prompts == [runner.REDACTION_STEP4_PROMPT]
    audit = json.loads((tmp_path / "redaction-audit.json").read_text(encoding="utf-8"))
    assert "real-generated-value" not in json.dumps(audit)
    assert any("no selection input was sent" in note for note in harness.notes)


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


def test_all_evidence_includes_workspace_text_files(tmp_path: Path) -> None:
    runner = _load_runner()
    workspace = tmp_path / "workspace"
    template_dir = workspace / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "main.yml").write_text(
        "Resources:\n  VSwitch:\n    Type: ALIYUN::ECS::VSwitch\n",
        encoding="utf-8",
    )
    (workspace / "ignored.bin").write_bytes(b"ALIYUN::ECS::VSwitch")
    harness = SimpleNamespace(
        summaries={},
        snapshots={},
        workspace_dir=workspace,
    )

    evidence = runner._all_evidence(harness)

    assert "templates/main.yml" in evidence
    assert "ALIYUN::ECS::VSwitch" in evidence
    assert "ignored.bin" not in evidence


def test_finish_pipeline_after_possible_input_uses_custom_prompt_for_pending_input() -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial = runner.StreamSummary(
        name="resume",
        prompt="继续",
        status_states=["TASK_STATE_INPUT_REQUIRED"],
        pipeline_event_types=["input_required"],
        last_input_required_step_id="intent_parsing",
    )

    def stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "continue-after-input-1"
        return runner.StreamSummary(
            name=name,
            prompt=prompt,
            status_states=["TASK_STATE_COMPLETED"],
            pipeline_event_types=["pipeline_completed"],
        )

    harness = SimpleNamespace(stream=stream)
    args = SimpleNamespace(selection_prompt="选择第一个方案")

    runner._finish_pipeline_after_possible_input(
        harness,
        initial,
        args,
        input_prompt=runner.ROLLBACK_PROMPT,
    )

    assert prompts == [runner.ROLLBACK_PROMPT]


def test_wait_for_with_intervening_ask_inputs_uses_custom_answer_prompt() -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial_summary = runner.StreamSummary(
        name="initial",
        prompt="",
        pipeline_event_types=["input_required"],
    )

    class InitialStream:
        name = "initial"
        events = [_input_required_event("ask_user_question")]

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("initial ended")

    class AnswerStream:
        name = "answer"
        events: list[dict] = []

        def wait_for(self, predicate, *, description: str, timeout: float):
            event = {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "input_required",
                                    "step": {"id": "confirm_and_select"},
                                    "data": {},
                                }
                            }
                        }
                    }
                }
            }
            if predicate(event, initial_summary):
                return runner.EventMatch(description=description, event=event, summary=initial_summary)
            raise TimeoutError(description)

    def start_stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "rollback-answer-ask-1"
        return AnswerStream()

    harness = SimpleNamespace(notes=[], start_stream=start_stream)

    streams = runner._wait_for_with_intervening_ask_inputs(
        harness,
        [InitialStream()],
        runner._input_required_step("confirm_and_select"),
        description="selection",
        timeout=1,
        name_prefix="rollback",
        answer_prompt=runner.ROLLBACK_PROMPT,
    )

    assert prompts == [runner.ROLLBACK_PROMPT]
    assert len(streams) == 2


def test_wait_for_with_intervening_inputs_answers_allowed_step_with_custom_prompt() -> None:
    runner = _load_runner()
    prompts: list[str] = []
    initial_summary = runner.StreamSummary(name="initial", prompt="", pipeline_event_types=["input_required"])

    class InitialStream:
        name = "initial"
        events = [_input_required_event(step_id="intent_parsing")]

        def wait_for(self, *_args, **_kwargs):
            raise RuntimeError("initial ended")

    class AnswerStream:
        name = "answer"
        events: list[dict] = []

        def wait_for(self, predicate, *, description: str, timeout: float):
            event = {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "step_started",
                                    "step": {"id": "confirm_and_select"},
                                    "data": {},
                                }
                            }
                        }
                    }
                }
            }
            if predicate(event, initial_summary):
                return runner.EventMatch(description=description, event=event, summary=initial_summary)
            raise TimeoutError(description)

    def start_stream(*, prompt: str, name: str):
        prompts.append(prompt)
        assert name == "rollback-answer-intent_parsing-1"
        return AnswerStream()

    harness = SimpleNamespace(notes=[], start_stream=start_stream)

    streams = runner._wait_for_with_intervening_ask_inputs(
        harness,
        [InitialStream()],
        runner._step_started("confirm_and_select"),
        description="confirm step",
        timeout=1,
        name_prefix="rollback",
        answer_prompt=runner.ROLLBACK_PROMPT,
        answer_input_steps={"intent_parsing"},
    )

    assert prompts == [runner.ROLLBACK_PROMPT]
    assert len(streams) == 2


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


def test_fault_after_snapshot_defaults_crash_point(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=True,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario="fault-after-snapshot")

    assert harness.server_env["IAC_CODE_TEST_CRASH_AT"] == runner.FAULT_AFTER_SNAPSHOT_POINT


def test_scenario1_performance_backup_configures_server_env(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario="scenario1-performance-backup")

    assert harness.server_env["IAC_CODE_MODEL"] == "deepseek-v4-flash-0731"
    assert harness.server_env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] == "true"
    assert harness.server_env["IAC_CODE_CONFIG_BACKUP_DIR"] == str((tmp_path / "run" / "session-backup").resolve())
    assert harness.backup_root == (tmp_path / "run" / "session-backup").resolve()


def test_selection_during_backup_configures_e2e_only_delay(tmp_path: Path) -> None:
    runner = _load_runner()
    args = SimpleNamespace(
        server_cwd=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        run_root=str(tmp_path),
        cwd="",
        host="127.0.0.1",
        port=0,
        no_auto_approve_permissions=False,
        provider="",
        model="",
        api_base="",
        deterministic=False,
        fault_at="",
    )

    harness = runner.ScenarioHarness(args, scenario=runner.SELECTION_DURING_BACKUP_SCENARIO)

    assert harness.server_env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] == "true"
    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] == "10.0"
    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] == str(
        (tmp_path / "run" / "selection-backup-delay").resolve()
    )
    assert str(runner.BACKUP_DELAY_FIXTURE_ROOT.resolve()) == harness.server_env["PYTHONPATH"].split(os.pathsep)[0]


def test_backup_delay_sitecustomize_delays_armed_input_required_backup(tmp_path: Path) -> None:
    runner = _load_runner()
    control = tmp_path / "backup-delay"
    runner._write_json(runner._backup_delay_marker_path(control, "arm"), {"armed": True})
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(runner.BACKUP_DELAY_FIXTURE_ROOT.resolve()), env.get("PYTHONPATH", "")) if value
    )
    env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] = "0.05"
    env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] = str(control)
    script = "\n".join(
        [
            "from iac_code.services.session_backup import BackupReason, SessionBackupService",
            "service = SessionBackupService()",
            "service.backup_session('', 'session-1', reason=BackupReason.INPUT_REQUIRED, critical=False)",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    finished = runner._wait_for_backup_delay_marker(control, "finished", timeout=1)
    assert finished["elapsedSeconds"] >= 0.05
    assert finished["succeeded"] is True


def test_scenario1_performance_backup_omits_selection_task_id_and_checks_backup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    harnesses = []

    class FakeHarness:
        def __init__(self, args) -> None:
            self.args = args
            self.run_dir = tmp_path
            self.workspace_dir = tmp_path / "workspace"
            self.workspace_dir.mkdir()
            self.server_env = {
                "IAC_CODE_A2A_EXTREME_PERFORMANCE": "true",
                "IAC_CODE_CONFIG_BACKUP_DIR": str(tmp_path / "backup"),
            }
            self.context_id = ""
            self.pipeline_task_id = ""
            self.checks = {}
            self.notes = []
            self.summaries = {}
            self.snapshots = {}
            self.stream_request_task_ids = {}
            self.kill9_count = 0
            self.start_server_count = 0

        def stream(
            self,
            *,
            prompt: str,
            name: str,
            context_id: str | None = None,
            task_id: str | None = None,
            **_kwargs,
        ):
            if context_id == "":
                self.context_id = "ctx-1"
            if not self.context_id:
                self.context_id = "ctx-1"
            if not self.pipeline_task_id:
                self.pipeline_task_id = "task-1"
            request_task_id = self.pipeline_task_id if task_id is None else task_id
            self.stream_request_task_ids[name] = request_task_id
            if name == "01-initial":
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=self.pipeline_task_id,
                    status_states=["TASK_STATE_INPUT_REQUIRED"],
                    pipeline_event_types=["input_required"],
                    last_input_required_step_id="confirm_and_select",
                )
            elif name == "02-select-candidate":
                restored_dir = tmp_path / "primary-session"
                restored_dir.mkdir(exist_ok=True)
                (restored_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=self.pipeline_task_id,
                    status_states=["TASK_STATE_COMPLETED"],
                    pipeline_event_types=["input_received", "step_completed", "pipeline_completed"],
                    normal_handoff_ready=True,
                    text="created ALIYUN::ECS::VSwitch",
                )
            else:
                summary = runner.StreamSummary(
                    name=name,
                    prompt=prompt,
                    request_task_id=request_task_id,
                    context_id=self.context_id,
                    task_id=f"{name}-task",
                    status_states=["TASK_STATE_COMPLETED"],
                    text="created ALIYUN::ECS::VSwitch",
                )
            self.summaries[name] = summary
            return summary

        def fetch_state(self, name: str):
            return {
                "snapshot": {
                    "contextId": self.context_id,
                    "taskId": self.pipeline_task_id,
                    "status": "completed",
                    "normalHandoff": {"action": "switch_to_normal", "targetMode": "normal"},
                }
            }

        def kill9_and_restart(self) -> None:
            pass

        def kill9(self) -> None:
            self.kill9_count += 1

        def start_server(self) -> None:
            self.start_server_count += 1

    def fake_run_with_harness(args, _scenario, callback):
        harness = FakeHarness(args)
        harnesses.append(harness)
        callback(harness)
        return 0 if all(harness.checks.values()) else 1

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(
        runner,
        "_waiting_input_backup_snapshots",
        lambda _h: {"task": {"state": "input-required"}, "context": {"active_task_id": None}},
    )
    monkeypatch.setattr(
        runner,
        "_remove_primary_session_for_backup_restore",
        lambda _h: {
            "primarySessionDir": str(tmp_path / "primary-session"),
            "primarySessionFile": str(tmp_path / "primary-session" / "session.jsonl"),
            "backupSessionDir": str(tmp_path / "backup-session"),
        },
    )
    (tmp_path / "backup-session").mkdir()
    monkeypatch.setattr(runner, "_a2a_session_contains_user_message", lambda _h, _text: True)
    monkeypatch.setattr(runner, "_all_evidence", lambda _h: "ALIYUN::ECS::VSwitch")
    monkeypatch.setattr(runner, "_run_dir_has_cleanup_events", lambda _run_dir: False)
    monkeypatch.setattr(runner, "_session_has_cleanup_prompt", lambda _h: False)
    monkeypatch.setattr(runner, "_cleanup_ledger_has_required_resources", lambda _h: False)
    args = SimpleNamespace(
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
        normal_followup_prompt=runner.DEFAULT_NORMAL_FOLLOWUP_PROMPT,
        recovery_prompt=runner.DEFAULT_RECOVERY_PROMPT,
    )

    assert runner.run_scenario1_performance_backup(args, "scenario1-performance-backup") == 0
    assert harnesses[0].stream_request_task_ids["02-select-candidate"] == ""
    assert harnesses[0].checks["selection omitted taskId"] is True
    assert harnesses[0].checks["selection hydrated recovered taskId"] is True
    assert harnesses[0].checks["step4 backup context has no active task"] is True
    assert harnesses[0].checks["primary session stayed absent after restart"] is True
    assert harnesses[0].checks["selection restored primary session from backup"] is True
    assert harnesses[0].kill9_count == 1
    assert harnesses[0].start_server_count == 1


def test_remove_primary_session_for_backup_restore_keeps_backup(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    run_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"
    context_id = "ctx-1"
    session_id = "session-1"
    workspace.mkdir()
    context_dir = run_dir / "a2a-persistence" / "contexts"
    context_dir.mkdir(parents=True)
    (context_dir / f"{context_id}.json").write_text(
        json.dumps({"context_id": context_id, "session_id": session_id, "cwd": str(workspace)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    primary_storage = SessionStorage(projects_dir=config_dir / "projects")
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    primary_storage.save(str(workspace), session_id, [Message(role="user", content="primary")])
    backup_storage.save(str(workspace), session_id, [Message(role="user", content="backup")])
    primary_session_dir = primary_storage.v2_session_dir(str(workspace), session_id)
    backup_session_dir = backup_storage.v2_session_dir(str(workspace), session_id)
    assert primary_session_dir is not None
    assert backup_session_dir is not None

    harness = SimpleNamespace(
        backup_root=backup_root,
        context_id=context_id,
        cwd=str(workspace),
        run_dir=run_dir,
    )
    evidence = runner._remove_primary_session_for_backup_restore(harness)

    assert evidence["primaryRemovedBeforeRestart"] is True
    assert evidence["backupPresentAfterRemoval"] is True
    assert not primary_session_dir.exists()
    assert backup_session_dir.is_dir()
    assert (run_dir / "step4.backup-only-restore.json").is_file()


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
    stack_name: str = "",
    cleared: bool = False,
) -> dict:
    data = {
        "provider": "ros",
        "action": action,
        "stackId": stack_id,
        "stackStatus": status,
        "isSuccess": is_success,
        "cleared": cleared,
    }
    if stack_name:
        data["stackName"] = stack_name
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "stack_current_changed",
                            "data": data,
                        }
                    }
                }
            }
        }
    }


def _ros_deploy_tool_result_event(
    *,
    stack_id: str,
    stack_name: str = "",
    status: str = "CREATE_COMPLETE",
    is_success: bool = True,
    is_error: bool = False,
) -> dict:
    result = {
        "stack_id": stack_id,
        "status": status,
        "is_success": is_success,
    }
    if stack_name:
        result["stack_name"] = stack_name
    return {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "tool_result",
                            "data": {
                                "toolName": "ros_deploy",
                                "isError": is_error,
                                "result": json.dumps(result),
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


def test_wait_for_created_stack_accepts_successful_continue_create_stack() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            status="CREATE_FAILED",
            is_success=False,
        ),
        _stack_current_changed_event(
            action="ContinueCreateStack",
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


def test_wait_for_created_stack_accepts_successful_ros_deploy_tool_result() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _ros_deploy_tool_result_event(stack_id="failed-stack", is_success=False),
        _ros_deploy_tool_result_event(stack_id="created-stack"),
    ]

    class FakeStream:
        name = "02-create-first-stack"

        def wait_for(self, predicate, *, description: str, timeout: float):
            for event in events:
                if predicate(event, summary):
                    return runner.EventMatch(description=description, event=event, summary=summary)
            raise TimeoutError(description)

    assert runner._wait_for_created_stack(FakeStream(), exclude=set(), timeout=1) == "created-stack"


def test_wait_for_created_stack_ignores_unexpected_stack_name() -> None:
    runner = _load_runner()
    summary = runner.StreamSummary(name="02-create-first-stack", prompt="deploy")
    events = [
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="wrong-stack",
            stack_name="wrong-name",
            status="CREATE_COMPLETE",
            is_success=True,
        ),
        _stack_current_changed_event(
            action="CreateStack",
            stack_id="created-stack",
            stack_name="expected-name",
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

    assert (
        runner._wait_for_created_stack(
            FakeStream(),
            exclude=set(),
            timeout=1,
            expected_stack_name="expected-name",
        )
        == "created-stack"
    )


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


def test_created_stack_id_from_stream_accepts_continue_create_stack_success() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _stack_current_changed_event(
                action="CreateStack",
                stack_id="created-stack",
                status="CREATE_FAILED",
                is_success=False,
            ),
            _stack_current_changed_event(
                action="ContinueCreateStack",
                stack_id="created-stack",
                status="CREATE_COMPLETE",
                is_success=True,
            ),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude=set()) == "created-stack"


def test_created_stack_id_from_stream_accepts_ros_deploy_tool_result_success() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _ros_deploy_tool_result_event(stack_id="failed-stack", is_error=True),
            _ros_deploy_tool_result_event(stack_id="created-stack"),
        ]
    )

    assert runner._created_stack_id_from_stream(stream, exclude=set()) == "created-stack"


def test_created_stack_id_from_stream_ignores_ros_deploy_tool_result_with_wrong_stack_name() -> None:
    runner = _load_runner()

    stream = SimpleNamespace(
        events=[
            _ros_deploy_tool_result_event(stack_id="wrong-stack", stack_name="wrong-name"),
            _ros_deploy_tool_result_event(stack_id="created-stack", stack_name="expected-name"),
        ]
    )

    assert (
        runner._created_stack_id_from_stream(stream, exclude=set(), expected_stack_name="expected-name")
        == "created-stack"
    )


def test_post_rollback_timeout_allows_step_regeneration_time() -> None:
    runner = _load_runner()

    args = SimpleNamespace(event_timeout=300, stream_timeout=2400)

    assert runner._post_rollback_timeout(args) == 900


def test_deterministic_fault_mode_still_runs_real_provider_preflight(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    args = runner.parse_args(
        [
            "--allow-real-cloud",
            "--deterministic",
            "--provider",
            "dashscope",
            "--run-dir",
            str(tmp_path),
            "--scenario",
            "fault-after-snapshot",
        ]
    )
    preflight = MagicMock(return_value={"ok": True, "summary": "ok"})
    monkeypatch.setattr(runner, "run_llm_preflight", preflight)
    harness = runner.ScenarioHarness(args, scenario="fault-after-snapshot")

    harness.preflight()

    preflight.assert_called_once()
    assert harness.checks["LLM preflight succeeded"] is True


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
    cleanup_started_snapshot = {"snapshot": {"cleanup": {"history": [{"eventType": "cleanup_started"}]}}}
    assert runner._snapshot_has_cleanup_activity(cleanup_started_snapshot) is True
    assert (
        runner._snapshot_has_cleanup_activity(
            {
                "snapshot": {
                    "cleanup": {
                        "status": "unavailable",
                        "resourceCount": 0,
                        "resources": [],
                        "history": [
                            {
                                "eventType": "pipeline_handoff_ready",
                                "status": "unavailable",
                                "data": {"status": "unavailable"},
                            }
                        ],
                    }
                }
            }
        )
        is False
    )


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
    intent = runner._cleanup_intent_prompt("创建一个 vswitch。", "iac-e2e-abcdef12-first")

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
    assert "创建一个 vswitch。" in intent
    assert "StackName 必须精确等于 `iac-e2e-abcdef12-first`" in intent
    assert "后续选择、模板生成、参数确认和部署步骤" in intent


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
                        stack_name=runner._cleanup_stack_name(self, "second"),
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
    first_stack_name = runner._cleanup_stack_name(harness, "first")
    second_stack_name = runner._cleanup_stack_name(harness, "second")
    assert first_stack_name in harness.stream_calls[0]["prompt"]
    assert second_stack_name in harness.summaries["03-rollback-after-first-stack"].prompt
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
                        stack_name=runner._cleanup_stack_name(self, "second"),
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
                        stack_name=runner._cleanup_stack_name(self, "second"),
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
                        stack_name=runner._cleanup_stack_name(self, "second"),
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

    finish_kwargs: list[dict] = []

    def fake_finish_pipeline_after_possible_input(*_args, **kwargs):
        finish_kwargs.append(kwargs)

    monkeypatch.setattr(runner, "_run_with_harness", fake_run_with_harness)
    monkeypatch.setattr(runner, "_wait_for_with_intervening_ask_inputs", lambda *args, **kwargs: [args[1][0]])
    monkeypatch.setattr(runner, "_wait_any", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_join_after_kill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_after_possible_input", fake_finish_pipeline_after_possible_input)
    monkeypatch.setattr(runner, "_completed_snapshot_or_stream", lambda *args, **kwargs: True)

    args = SimpleNamespace(
        event_timeout=1,
        initial_prompt=runner.DEFAULT_INITIAL_PROMPT,
        selection_prompt=runner.DEFAULT_SELECTION_PROMPT,
    )

    assert runner.run_rollback(args, "rollback-step1") == 0
    assert finish_kwargs == [{"input_prompt": runner.ROLLBACK_PROMPT}]


def test_final_deployment_evidence_uses_handoff_target_when_deploy_failed() -> None:
    runner = _load_runner()
    handoff_context = {
        "intent": {
            "core_requirements": ["VPC", "VSwitch"],
            "resource_intents": [
                {"product": "VPC", "action": "use_existing"},
                {"product": "VSwitch", "action": "create"},
            ],
        },
        "architecture": {
            "candidates": [
                {
                    "name": "已有VPC创建安全组",
                    "products": ["VPC", "SecurityGroup"],
                    "resource_intents": [
                        {
                            "resource_type": "ALIYUN::ECS::SecurityGroup",
                            "action": "create",
                        }
                    ],
                    "cons": ["不提供 VSwitch 等网络基础设施"],
                }
            ]
        },
        "evaluated_candidates": [{"template_path": "templates/1-existing-vpc-security-group.yml"}],
        "selected_plan": {
            "selected_candidate": {
                "products": ["SecurityGroup"],
                "resource_intents": [
                    {"product": "VPC", "action": "use_existing"},
                    {"product": "SecurityGroup", "action": "create"},
                    {"product": "VSwitch", "action": "forbid"},
                ],
            },
            "resource_types": ["ALIYUN::ECS::SecurityGroup"],
        },
        "deployment": {"status": "failed", "error": "STS token exchange denied"},
    }
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        f"{json.dumps(handoff_context, ensure_ascii=False)}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [
                {
                    "id": "deploying",
                    "status": "completed",
                    "conclusion": {"status": "failed", "error": "STS token exchange denied"},
                }
            ],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    evidence = runner._final_deployment_evidence(final_state)

    assert "SecurityGroup" in evidence
    assert "VSwitch" not in evidence


def test_final_deployment_evidence_prefers_realized_target_over_stale_candidate() -> None:
    runner = _load_runner()
    stale_candidate = {
        "name": "已有 VPC 中创建 VSwitch",
        "output_path": "templates/1-existing-vpc-create-vswitch.yml",
        "products": ["VPC", "VSwitch"],
        "resource_intents": [
            {"product": "VPC", "action": "use_existing"},
            {"product": "VSwitch", "action": "create"},
        ],
        "topology": "在已有 VPC 中创建一个 VSwitch。",
    }
    handoff_context = {
        "selected_plan": {
            "selected_candidate_name": stale_candidate["name"],
            "selected_candidate": stale_candidate,
            "selected_candidate_result": {
                "candidate": stale_candidate,
                "failed": False,
                "template": {
                    "template": (
                        "ROSTemplateFormatVersion: '2015-09-01'\n"
                        "Resources:\n"
                        "  SecurityGroup:\n"
                        "    Type: ALIYUN::ECS::SecurityGroup\n"
                    ),
                    "file_path": "templates/1-existing-vpc-create-security-group.yml",
                    "region": "cn-hangzhou",
                    "description": "在已有 VPC 中创建安全组",
                },
                "cost": {
                    "resources": [{"type": "ALIYUN::ECS::SecurityGroup", "cost": "¥0"}],
                    "deployment_parameters": {
                        "RegionId": "cn-hangzhou",
                        "VpcId": "vpc-test",
                        "SecurityGroupName": "sg-test",
                    },
                    "preview_validation": {
                        "succeeded": True,
                        "template_url": "templates/1-existing-vpc-create-security-group.yml",
                    },
                },
            },
        },
        "deployment": {
            "resources_created": ["ALIYUN::ECS::SecurityGroup"],
            "stack_id": "stack-test",
            "status": "success",
            "outputs": {"SecurityGroupId": "sg-test"},
        },
    }
    handoff_summary = (
        "[Pipeline Handoff Context]\n"
        "This is injected context for the assistant, not a user request.\n"
        "Pipeline: selling\n"
        "Outcome: completed\n\n"
        "Included context:\n"
        f"{json.dumps(handoff_context, ensure_ascii=False)}\n\n"
        "Use this context when answering follow-up questions after the pipeline handoff."
    )
    final_state = {
        "snapshot": {
            "steps": [{"id": "deploying", "status": "completed", "conclusion": {"status": "success"}}],
            "normalHandoff": {"summary": handoff_summary},
        }
    }

    evidence = runner._final_deployment_evidence(final_state)

    assert "SecurityGroup" in evidence
    assert "VSwitch" not in evidence
