"""Ownership checks for CI cleanup of real A2A recovery stacks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.a2a.e2e.cleanup_owned_stacks import cleanup_owned_stacks


@pytest.mark.parametrize("code", ["EntityNotExist.Stack", "NotFound.Stack", "StackNotFound"])
def test_get_stack_missing_sdk_code_means_deleted(code):
    from scripts.a2a.e2e.cleanup_owned_stacks import _stack_body

    error = RuntimeError("private cloud response")
    error.code = code

    def get_stack(_request):
        raise error

    models = SimpleNamespace(GetStackRequest=lambda **kw: SimpleNamespace(**kw))
    assert _stack_body(SimpleNamespace(get_stack=get_stack), models, "fake-id", "cn-hangzhou") is None


def test_get_stack_credential_error_cannot_be_misclassified_as_deleted():
    from scripts.a2a.e2e.cleanup_owned_stacks import _stack_body

    error = RuntimeError("Forbidden.RAM: diagnostic mentions EntityNotExist.Stack")
    error.code = "Forbidden.RAM"

    def get_stack(_request):
        raise error

    models = SimpleNamespace(GetStackRequest=lambda **kw: SimpleNamespace(**kw))
    with pytest.raises(RuntimeError, match="Forbidden.RAM"):
        _stack_body(SimpleNamespace(get_stack=get_stack), models, "fake-id", "cn-hangzhou")


def test_cleanup_failure_diagnostic_never_exports_cloud_body(tmp_path):
    from scripts.a2a.e2e.cleanup_owned_stacks import _record_cleanup_failure

    error = RuntimeError("sk-private credential and stack-name")
    error.code = "Forbidden.RAM"
    _record_cleanup_failure(tmp_path, "delete_stack", error)
    data = json.loads((tmp_path / "cleanup-cloud.log").read_text(encoding="utf-8"))
    assert data == {"cleanupDiagnostic": {"stage": "delete_stack", "errorType": "SDKError", "code": "Forbidden.RAM"}}
    assert "sk-private" not in json.dumps(data)


def _manifest(run_dir: Path, name: str = "model-chosen-network") -> None:
    import yaml

    from iac_code.services.session_storage import SessionStorage

    config = run_dir / "config"
    cwd = str(run_dir / "workspace")
    contexts = run_dir / "a2a-persistence" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "ctx-1.json").write_text(json.dumps({"session_id": "session-1", "cwd": cwd}), encoding="utf-8")
    directory = SessionStorage(projects_dir=config / "projects").session_dir(cwd, "session-1") / "pipeline"
    directory.mkdir(parents=True)
    resource = {
        "provider": "ros", "resource_type": "stack", "resource_id": "owned-id",
        "resource_name": name, "region_id": "cn-hangzhou", "source_step_id": "deploying",
        "source_attempt_id": "att_001", "observed_action": "CreateStack",
        "metadata": {"tool_name": "ros_stack", "tool_use_id": "create-1"},
    }
    (directory / "cleanup.yaml").write_text(yaml.safe_dump({"observed_resources": [resource]}), encoding="utf-8")
    (directory / "meta.yaml").write_text(yaml.safe_dump({
        "attempts": {"items": {"att_001": {"step_id": "deploying"}}},
    }), encoding="utf-8")
    (run_dir / "owned-stacks.json").write_text(json.dumps({"configDir": str(config), "cwd": cwd}), encoding="utf-8")


def test_name_only_manifest_cannot_authorize_deletion(tmp_path):
    (tmp_path / "owned-stacks.json").write_text(json.dumps({
        "runId": "123456789abc", "stackNames": ["iac-e2e-123456789abc-main"],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="ownership"):
        cleanup_owned_stacks(tmp_path)


@pytest.mark.parametrize("actual_name", ["model-chosen-network", "another-account-resource"])
def test_cleanup_uses_accepted_id_and_actual_name_not_test_prefix(tmp_path, monkeypatch, actual_name):
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    _manifest(tmp_path)
    deleted = []

    class Client:
        def list_stacks(self, _request):
            pytest.fail("shared-account name discovery cannot authorize deletion")

        def get_stack(self, request):
            assert request.stack_id == "owned-id"
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                "StackName": actual_name, "Status": "DELETE_COMPLETE" if deleted else "CREATE_COMPLETE",
            }))

        def delete_stack(self, request):
            deleted.append(request.stack_id)

    monkeypatch.setattr(CloudCredentials, "get_provider", lambda *_: SimpleNamespace(region_id="cn-hangzhou"))
    monkeypatch.setattr(RosClientFactory, "create", lambda *_: Client())
    monkeypatch.setattr("scripts.a2a.e2e.cleanup_owned_stacks.time.sleep", lambda _: None)
    result = cleanup_owned_stacks(tmp_path, timeout=5)
    ours = actual_name == "model-chosen-network"
    assert result["status"] == ("completed" if ours else "failed")
    assert deleted == (["owned-id"] if ours else [])
    assert result["remainingStackIds"] == ([] if ours else ["owned-id"])


@pytest.mark.parametrize("real_event", [True, False])
def test_observed_foreign_stack_is_audited_but_never_deleted(tmp_path, monkeypatch, real_event):
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    _manifest(tmp_path)
    event = {"eventType": "stack_current_changed", "data": {"stackId": "foreign-id"}}
    row = {"pipelineEvent": event} if real_event else {"text": json.dumps(event)}
    (tmp_path / "initial.events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    class Client:
        def get_stack(self, request):
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                "StackName": "model-chosen-network" if request.stack_id == "owned-id" else "foreign-name",
                "Status": "DELETE_COMPLETE" if request.stack_id == "owned-id" else "CREATE_COMPLETE",
            }))

        def delete_stack(self, _request):
            pytest.fail("observing an ID does not authorize deletion")

    monkeypatch.setattr(CloudCredentials, "get_provider", lambda *_: SimpleNamespace(region_id="cn-hangzhou"))
    monkeypatch.setattr(RosClientFactory, "create", lambda *_: Client())
    result = cleanup_owned_stacks(tmp_path)
    assert result["status"] == ("failed" if real_event else "completed")
    assert result["remainingStackIds"] == (["foreign-id"] if real_event else [])
