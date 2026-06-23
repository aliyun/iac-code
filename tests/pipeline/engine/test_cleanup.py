from __future__ import annotations

import json
import logging
import threading
import time

import yaml

import iac_code.pipeline.engine.cleanup as cleanup_module
from iac_code.pipeline.engine.cleanup import (
    CleanupLedger,
    CleanupObserver,
    CleanupResource,
    ObservedResource,
    cleanup_prompt_ledger_path,
    create_cleanup_prompt_message,
    is_active_cleanup_prompt_message,
    mark_cleanup_prompt_message_completed,
)
from iac_code.types.stream_events import StackProgressEvent, ToolResultEvent, ToolUseEndEvent


def _observed_stack() -> ObservedResource:
    return ObservedResource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        resource_name="demo",
        region_id="cn-hangzhou",
        source_step_id="deploying",
        source_attempt_id="att_0001",
        observed_action="CreateStack",
        observed_at=1.0,
        metadata={"tool_name": "ros_stack"},
    )


def test_ledger_persists_observed_and_required_resources(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    observed = _observed_stack()

    ledger.record_observed(observed)
    ledger.mark_cleanup_required(
        [CleanupResource.from_observed(observed, reason="rollback requested")],
        source_step_id="deploying",
        reason="rollback requested",
    )

    restored = CleanupLedger(tmp_path / "cleanup.yaml")
    assert restored.observed_resources()[0] == observed
    pending = restored.pending_resources()
    assert len(pending) == 1
    assert pending[0].provider == "ros"
    assert pending[0].resource_type == "stack"
    assert pending[0].resource_id == "stack-123"
    assert pending[0].resource_name == "demo"
    assert pending[0].region_id == "cn-hangzhou"
    assert pending[0].cleanup_status == "pending"
    assert pending[0].cleanup_required is True


def test_pending_prompt_includes_active_resources_after_restart(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resources = [
        CleanupResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-started",
            region_id="cn-hangzhou",
            cleanup_status="started",
            progress_status="DELETE_STARTED",
        ),
        CleanupResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-progress",
            region_id="cn-hangzhou",
            cleanup_status="in_progress",
            progress_status="DELETE_IN_PROGRESS",
        ),
        CleanupResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-complete",
            region_id="cn-hangzhou",
            cleanup_status="completed",
            progress_status="DELETE_COMPLETE",
        ),
    ]
    ledger.mark_cleanup_required(resources, source_step_id="deploying", reason="rollback requested")

    prompt = ledger.build_pending_prompt()

    assert prompt is not None
    assert [resource.resource_id for resource in prompt.resources] == ["stack-started", "stack-progress"]
    assert "stack-started" in prompt.prompt
    assert "stack-progress" in prompt.prompt
    assert "stack-complete" not in prompt.prompt
    assert "严格白名单" in prompt.prompt
    assert "只能删除下面“待清理资源”列表中的 id" in prompt.prompt
    assert "不要删除、修改或回滚任何未列入“待清理资源”的 stack 或云资源" in prompt.prompt
    assert "不要调用 ListStacks 或按名称搜索其它 stack" in prompt.prompt
    assert "必须核对 StackId 精确等于“待清理资源”列表中的某个 id" in prompt.prompt
    assert "如果 StackId 不在“待清理资源”列表中，禁止调用 DeleteStack" in prompt.prompt
    assert (
        "不要根据 pipeline handoff、deployment.stack_id、current stack 或 resources_created 额外推断清理对象"
        in prompt.prompt
    )
    assert "即使本轮还有用户追问、继续指令或 pipeline handoff 上下文，也不能扩大清理范围" in prompt.prompt
    assert "恢复或继续清理时仍只处理当前提示列出的资源" in prompt.prompt
    assert "如果用户只说“继续”" not in prompt.prompt
    assert "列表内资源全部 DELETE_COMPLETE 后，立刻停止本轮清理；不要继续删除或检查任何其他 stack" in prompt.prompt


def test_ledger_records_prompt_queued_history(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    prompt = ledger.build_pending_prompt()
    assert prompt is not None

    ledger.record_prompt_queued(prompt, ui_surface="repl")

    history = ledger._load()["history"]
    assert [entry["type"] for entry in history] == ["cleanup_required", "cleanup_prompt_queued"]
    assert history[-1]["ui_surface"] == "repl"
    assert history[-1]["resource_count"] == 1
    assert history[-1]["resources"][0]["resource_id"] == "stack-123"
    assert "prompt" not in history[-1]


def test_cleanup_prompt_message_tracks_ledger_path_and_completion(tmp_path) -> None:
    path = tmp_path / "cleanup.yaml"
    message = create_cleanup_prompt_message(
        "cleanup hidden prompt",
        cleanup_ledger_path=path,
        cleanup_status="pending",
    )

    assert cleanup_prompt_ledger_path(message) == str(path)
    assert is_active_cleanup_prompt_message(message)

    assert mark_cleanup_prompt_message_completed(message, cleanup_ledger_path=path) is True

    assert message.metadata["cleanupStatus"] == "completed"
    assert not is_active_cleanup_prompt_message(message)


def test_observer_marks_ros_stack_delete_complete(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123", "StackName": "demo"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps(
                {
                    "stack_id": "stack-123",
                    "stack_name": "demo",
                    "status": "DELETE_COMPLETE",
                    "is_success": True,
                }
            ),
            is_error=False,
        )
    )

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "completed"
    assert updated.cleanup_tool_use_id == "toolu-delete"
    assert updated.progress_status == "DELETE_COMPLETE"


def test_observer_keeps_statusless_delete_stack_result_in_progress(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "is_success": True}),
            is_error=False,
        )
    )

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "in_progress"
    assert updated.progress_status == "DELETE_REQUESTED"
    assert ledger.build_pending_prompt() is not None


def test_observer_marks_ros_stack_delete_failed(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "status": "DELETE_FAILED", "is_success": False}),
            is_error=True,
        )
    )

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "failed"
    assert updated.progress_status == "DELETE_FAILED"
    assert updated.last_error


def test_update_resource_sanitizes_durable_last_error(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")

    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="failed",
        progress_status="DELETE_FAILED",
        last_error=(
            "AccessKeySecret=super-secret token=sk-live-1234567890 "
            "Authorization: Bearer bearer-secret at /Users/alice/.iac-code/settings.yml"
        ),
    )

    data = ledger._load()
    resource_error = data["cleanup_resources"][0]["last_error"]
    history_error = data["history"][-1]["last_error"]
    for value in (resource_error, history_error):
        assert "super-secret" not in value
        assert "sk-live" not in value
        assert "bearer-secret" not in value
        assert "/Users/alice" not in value
        assert "[REDACTED]" in value or "[PATH]" in value


def test_observer_tracks_aliyun_api_delete_then_get_stack_polling(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="aliyun_api",
            result=json.dumps({"RequestId": "req-1"}),
            is_error=False,
        )
    )
    [started] = ledger.cleanup_resources()
    assert started.cleanup_status == "in_progress"
    assert started.progress_status == "DELETE_REQUESTED"

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-get-1",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-get-1",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "Status": "DELETE_IN_PROGRESS"}),
            is_error=False,
        )
    )
    [progress] = ledger.cleanup_resources()
    assert progress.cleanup_status == "in_progress"
    assert progress.progress_status == "DELETE_IN_PROGRESS"

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-get-2",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-get-2",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "Status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )
    [completed] = ledger.cleanup_resources()
    assert completed.cleanup_status == "completed"
    assert completed.progress_status == "DELETE_COMPLETE"


def test_observer_clears_previous_error_after_retry_success(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="failed",
        last_error="DELETE_FAILED",
    )
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-get",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-get",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "Status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    [completed] = ledger.cleanup_resources()
    assert completed.cleanup_status == "completed"
    assert completed.progress_status == "DELETE_COMPLETE"
    assert completed.last_error is None


def test_terminal_cleanup_resource_ignores_late_nonterminal_or_failed_events(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-late-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-late-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "is_success": True}),
            is_error=False,
        )
    )
    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-late-get",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )
    )
    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-late-get",
            tool_name="aliyun_api",
            result=json.dumps({"StackId": "stack-123", "Status": "DELETE_FAILED"}),
            is_error=True,
        )
    )

    [completed] = ledger.cleanup_resources()
    assert completed.cleanup_status == "completed"
    assert completed.progress_status == "DELETE_COMPLETE"
    assert completed.cleanup_tool_use_id is None
    assert completed.last_error is None
    assert ledger.build_pending_prompt() is None


def test_mark_cleanup_required_skips_terminal_resources_without_history(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-completed",
                region_id="cn-hangzhou",
                cleanup_status="completed",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-skipped",
                region_id="cn-hangzhou",
                cleanup_status="skipped",
            ),
        ],
        source_step_id="deploying",
        reason="rollback requested",
    )
    history_before = list(ledger._load()["history"])

    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-completed",
                region_id="cn-hangzhou",
                cleanup_status="pending",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-skipped",
                region_id="cn-hangzhou",
                cleanup_status="pending",
            ),
        ],
        source_step_id="deploying",
        reason="rollback requested again",
    )

    assert ledger._load()["history"] == history_before
    assert [resource.cleanup_status for resource in ledger.cleanup_resources()] == ["completed", "skipped"]


def test_mark_cleanup_required_preserves_active_execution_fields(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        cleanup_tool_use_id="toolu-delete",
        cleanup_action="DeleteStack",
        progress_status="DELETE_IN_PROGRESS",
        progress_percentage=30,
        last_error="slow",
    )

    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested again")

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "in_progress"
    assert updated.cleanup_tool_use_id == "toolu-delete"
    assert updated.cleanup_action == "DeleteStack"
    assert updated.progress_status == "DELETE_IN_PROGRESS"
    assert updated.progress_percentage == 30
    assert updated.last_error == "slow"


def test_observer_uses_persisted_tool_mapping_after_restart(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    CleanupObserver(ledger).observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
    )

    restarted = CleanupObserver(CleanupLedger(tmp_path / "cleanup.yaml"))
    restarted.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    [updated] = CleanupLedger(tmp_path / "cleanup.yaml").cleanup_resources()
    assert updated.cleanup_status == "completed"


def test_observer_rejects_persisted_mapping_result_stack_id_mismatch(tmp_path, caplog) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(provider="ros", resource_type="stack", resource_id="stack-a", region_id="cn-hangzhou"),
            CleanupResource(provider="ros", resource_type="stack", resource_id="stack-b", region_id="cn-hangzhou"),
        ],
        source_step_id="deploying",
        reason="rollback requested",
    )
    ledger.record_tool_use_mapping(
        tool_use_id="toolu-delete-a",
        provider="ros",
        resource_type="stack",
        resource_id="stack-a",
        region_id="cn-hangzhou",
        action="DeleteStack",
        tool_name="ros_stack",
        tool_input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-a"}},
    )
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.cleanup")
    unsafe_stack_id = "stack-b AccessKeySecret=super-secret /Users/alice/.iac-code/settings.yml"

    CleanupObserver(CleanupLedger(tmp_path / "cleanup.yaml")).observe(
        ToolResultEvent(
            tool_use_id="toolu-delete-a",
            tool_name="ros_stack",
            result=json.dumps({"StackId": unsafe_stack_id, "Status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    resources = {
        resource.resource_id: resource for resource in CleanupLedger(tmp_path / "cleanup.yaml").cleanup_resources()
    }
    assert resources["stack-a"].cleanup_status == "pending"
    assert resources["stack-b"].cleanup_status == "pending"
    history = CleanupLedger(tmp_path / "cleanup.yaml").history_entries()
    assert history[-1]["type"] == "cleanup_tool_result_mismatch"
    assert history[-1]["tool_use_id"] == "toolu-delete-a"
    assert history[-1]["mapped_resource_id"] == "stack-a"
    assert history[-1]["result_resource_id"] != unsafe_stack_id
    assert "super-secret" not in history[-1]["result_resource_id"]
    assert "/Users/alice" not in history[-1]["result_resource_id"]
    assert "[REDACTED]" in history[-1]["result_resource_id"]
    assert "[PATH]" in history[-1]["result_resource_id"]
    assert history[-1]["tool_name"] == "ros_stack"
    assert "Mismatched cleanup tool result" in caplog.text
    assert "super-secret" not in caplog.text
    assert "/Users/alice" not in caplog.text
    assert "settings.yml" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "[PATH]" in caplog.text


def test_observer_rejects_in_memory_tool_result_stack_id_mismatch(tmp_path, caplog) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(provider="ros", resource_type="stack", resource_id="stack-a", region_id="cn-hangzhou"),
            CleanupResource(provider="ros", resource_type="stack", resource_id="stack-b", region_id="cn-hangzhou"),
        ],
        source_step_id="deploying",
        reason="rollback requested",
    )
    observer = CleanupObserver(ledger)
    observer.observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete-a",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-a"}},
        )
    )
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.cleanup")

    observer.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete-a",
            tool_name="ros_stack",
            result=json.dumps({"StackId": "stack-b", "Status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    resources = {resource.resource_id: resource for resource in ledger.cleanup_resources()}
    assert resources["stack-a"].cleanup_status == "started"
    assert resources["stack-b"].cleanup_status == "pending"
    assert ledger.history_entries()[-1]["type"] == "cleanup_tool_result_mismatch"
    assert "Mismatched cleanup tool result" in caplog.text


def test_observer_records_history_warning_for_unmatched_cleanup_tool_result(tmp_path, caplog) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.cleanup")

    CleanupObserver(ledger).observe(
        ToolResultEvent(
            tool_use_id="toolu-missing",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    [pending] = ledger.cleanup_resources()
    assert pending.cleanup_status == "pending"
    history = ledger.history_entries()
    assert history[-1]["type"] == "cleanup_tool_result_unmatched"
    assert history[-1]["tool_use_id"] == "toolu-missing"
    assert history[-1]["tool_name"] == "ros_stack"
    assert "Unmatched cleanup tool result" in caplog.text


def test_corrupt_ledger_records_unavailable_without_overwrite(tmp_path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    ledger.mark_cleanup_required(
        [CleanupResource.from_observed(_observed_stack(), reason="rollback")],
        source_step_id="deploying",
        reason="rollback",
    )

    assert path.read_text(encoding="utf-8") == "[broken"
    assert ledger.load_failed()
    assert ledger.load_error()


def test_cleanup_ledger_save_uses_state_io_atomic_durable_write(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_atomic_write_text(path, content, *, durable=True, **_kwargs):
        calls.append((path, content, durable))
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(cleanup_module, "atomic_write_text", fake_atomic_write_text, raising=False)
    path = tmp_path / "cleanup.yaml"
    ledger = CleanupLedger(path)

    ledger.mark_cleanup_required(
        [CleanupResource.from_observed(_observed_stack(), reason="rollback requested")],
        source_step_id="deploying",
        reason="rollback requested",
    )

    assert len(calls) == 1
    saved_path, content, durable = calls[0]
    assert saved_path == path
    assert durable is True
    saved = yaml.safe_load(content)
    assert saved["cleanup_resources"][0]["resource_id"] == "stack-123"


def test_mark_cleanup_required_serializes_read_modify_write_for_same_path(tmp_path) -> None:
    first_save_entered = threading.Event()
    save_lock = threading.Lock()
    save_count = 0

    class SlowFirstSaveLedger(CleanupLedger):
        def _save(self, data):
            nonlocal save_count
            with save_lock:
                save_count += 1
                current_save = save_count
            if current_save == 1:
                first_save_entered.set()
                time.sleep(0.25)
            super()._save(data)

    path = tmp_path / "cleanup.yaml"
    errors = []

    def mark_required(resource_id: str) -> None:
        try:
            resource = CleanupResource.from_observed(
                ObservedResource(
                    provider="ros",
                    resource_type="stack",
                    resource_id=resource_id,
                    region_id="cn-hangzhou",
                    source_step_id="deploying",
                    observed_action="CreateStack",
                ),
                reason="rollback requested",
            )
            SlowFirstSaveLedger(path).mark_cleanup_required(
                [resource],
                source_step_id="deploying",
                reason="rollback requested",
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    first = threading.Thread(target=mark_required, args=("stack-one",))
    second = threading.Thread(target=mark_required, args=("stack-two",))

    first.start()
    assert first_save_entered.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    resources = CleanupLedger(path).cleanup_resources()
    assert sorted(resource.resource_id for resource in resources) == ["stack-one", "stack-two"]


def test_corrupt_ledger_non_empty_writes_do_not_mutate_or_replace_file(tmp_path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    ledger.record_observed(_observed_stack())
    ledger.mark_cleanup_required(
        [CleanupResource.from_observed(_observed_stack(), reason="rollback requested")],
        source_step_id="deploying",
        reason="rollback requested",
    )

    assert path.read_text(encoding="utf-8") == "[broken"
    assert not list(tmp_path.glob("cleanup.yaml.corrupt*"))
    assert ledger.load_failed() is True
    assert ledger.observed_resources() == []
    assert ledger.cleanup_resources() == []


def test_corrupt_ledger_update_does_not_write_empty_replacement(tmp_path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    assert ledger.load_failed() is True
    assert ledger.load_error()

    changed = ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="completed",
    )

    assert changed is False
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "[broken"
    assert not list(tmp_path.glob("cleanup.yaml.corrupt*"))


def test_observer_updates_progress_from_stack_progress_event(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    observer = CleanupObserver(ledger)

    observer.observe(
        StackProgressEvent(
            stack_id="stack-123",
            stack_name="demo",
            status="DELETE_IN_PROGRESS",
            progress_percentage=60,
            resources=[],
            elapsed_seconds=12,
        )
    )

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "in_progress"
    assert updated.progress_status == "DELETE_IN_PROGRESS"
    assert updated.progress_percentage == 60
