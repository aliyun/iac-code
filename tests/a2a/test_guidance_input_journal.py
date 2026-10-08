import pytest


def test_guidance_claim_is_durable_and_ambiguous_retry_cannot_apply_twice(monkeypatch, tmp_path):
    from iac_code.a2a.guidance_input import GuidanceInputConflictError, GuidanceInputJournal

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    journal = GuidanceInputJournal()
    identity = dict(context_id="ctx-1", task_id="task-1", guidance_id="guide-1", input_digest="d" * 64)
    assert journal.claim(**identity)["status"] == "STARTED"
    with pytest.raises(GuidanceInputConflictError):
        GuidanceInputJournal().claim(**identity)
    journal.mark_applied(**identity)
    assert GuidanceInputJournal().claim(**identity)["status"] == "APPLIED"
    with pytest.raises(GuidanceInputConflictError):
        journal.claim(**(identity | {"input_digest": "e" * 64}))


@pytest.mark.asyncio
async def test_handoff_delivery_checks_image_bytes_and_uses_current_caller_credentials(monkeypatch):
    import base64
    import hashlib
    from types import SimpleNamespace

    from iac_code.a2a.handoff import HandoffInputRequest, MigrationReceipt, PhysicalExecutionIdentity
    from iac_code.a2a.handoff_input import HandoffInputDelivery

    content = b"image-checkpoint-content"
    descriptor = {
        "filename": "diagram.png",
        "media_type": "image/png",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    pending = {"query": "continue", "image_attachments": {"items": [descriptor]}}
    receipt = MigrationReceipt(
        user_id="user-1",
        backend_scope="acs",
        session_id="session-1",
        cwd="/workspace/session-1",
        internal_session_id="internal-1",
        context_id="ctx-1",
        task_id="task-1",
        protocol="a2a",
        mode="pipeline",
        source=PhysicalExecutionIdentity(
            sandbox_id="source", activation_id="source-a", run_id="run-1", lease_id="source-l"
        ),
        migration_id="migration-1",
        epoch=2,
        commit_id="checkpoint-1",
        manifest_digest="a" * 64,
        backup_generation=2,
        business_revision=7,
        source_quiesced=True,
        shared_committed=True,
        recovery_kind="input_required",
        pending_input=pending,
        guidance_id="guide-1",
        input_digest="d" * 64,
    )
    target = PhysicalExecutionIdentity(
        sandbox_id="target", activation_id="target-a", run_id="run-1", lease_id="target-l"
    )
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def stream_message_parts(self, url, parts, **kwargs):
            calls.append((parts, kwargs))
            yield {"id": "task-1", "contextId": "ctx-1"}

        async def aclose(self):
            pass

    monkeypatch.setattr("iac_code.a2a.handoff_input.A2AClient", Client)
    caller = {"iac_code": {"alibaba_cloud_access_key_id": "fake-current-caller", "caller_identity": "current"}}
    request = HandoffInputRequest(
        receipt=receipt,
        target=target,
        input_digest="d" * 64,
        caller_metadata=caller,
        pending_input=pending | {"image_parts": [{"bytes_base64": base64.b64encode(content).decode()}]},
    )
    delivery = HandoffInputDelivery("http://target", SimpleNamespace())
    await delivery.apply(request)
    parts, options = calls[0]
    assert parts[1]["data"]["bytes"] == base64.b64encode(content).decode()
    assert options["context_id"] == "ctx-1" and options["task_id"] == "task-1"
    metadata = options["iac_code_metadata"]
    assert metadata["alibaba_cloud_access_key_id"] == caller["iac_code"]["alibaba_cloud_access_key_id"]
    assert metadata["execution_fence"]["owner"] == target.model_dump(mode="json", by_alias=True)
    assert "guidance_id" not in caller["iac_code"]
    corrupted = request.model_copy(
        update={"pending_input": pending | {"image_parts": [{"bytes_base64": "Y29ycnVwdA=="}]}}
    )
    with pytest.raises(ValueError, match="Image checkpoint digest changed"):
        await delivery.apply(corrupted)
    assert len(calls) == 1


@pytest.mark.parametrize("status,expected", [(None, "UNKNOWN"), ("STARTED", "UNKNOWN"), ("APPLIED", "APPLIED")])
def test_unknown_submission_checkpoint_never_guesses_unapplied(monkeypatch, tmp_path, status, expected):
    import hashlib
    from types import SimpleNamespace

    from iac_code.a2a.handoff import SessionHandoffService
    from iac_code.utils.state_io import atomic_write_json

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    request = SimpleNamespace(
        context_id="ctx-1",
        task_id="task-1",
        guidance_id="guide-1",
        input_digest="d" * 64,
        input_application_unknown=True,
    )
    service = SessionHandoffService(task_store=None, controls=None, persistence_root=tmp_path / "a2a")
    snapshot = tmp_path / "snapshot"
    if status is not None:
        path = snapshot / "guidance" / (hashlib.sha256(b"guide-1").hexdigest() + ".json")
        atomic_write_json(
            path,
            {
                "context_id": "ctx-1",
                "task_id": "task-1",
                "guidance_id": "guide-1",
                "input_digest": "d" * 64,
                "status": status,
            },
        )
    assert service._input_acceptance(request, snapshot) == expected
