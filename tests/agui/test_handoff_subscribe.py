import json

from starlette.testclient import TestClient

from iac_code.agui.adapter import AguiA2AAdapter, ThreadBinding
from iac_code.agui.app import create_app


class ObserveOnlyClient:
    def __init__(self):
        self.observed = []

    async def get_task(self, url, task_id, **kwargs):
        self.observed.append(task_id)
        return {"id": task_id, "contextId": "ctx-1", "status": {"state": "completed"}}

    async def cancel_task(self, *args, **kwargs):
        raise AssertionError("An observer cannot cancel the execution")

    async def aclose(self):
        pass


def test_subscribe_observes_original_execution_without_admitting_or_applying_input(tmp_path):
    upstream = ObserveOnlyClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a", client=upstream, state_dir=tmp_path)
    binding = ThreadBinding(
        thread_id="thread-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        user_id="user-1",
        ros_invocation_id="inv-1",
        iac_code_session_id="internal-1",
        execution_id="exec-1",
        task_id="task-1",
        run_digests={"run-1": "digest-1"},
    )
    adapter._persist_thread(binding)
    before = adapter._state_store.load_thread("thread-1")
    app = create_app(adapter=adapter, auth_token="token")
    with TestClient(app) as client:
        response = client.post(
            "/extensions/iac-code/v1/executions/exec-1/subscribe",
            headers={"Authorization": "Bearer token"},
            json={"threadId": "thread-1", "runId": "run-1", "rosInvocationId": "inv-1"},
        )
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert any(event.get("name") == "iac-code.session.v1" for event in events)
    assert upstream.observed == ["task-1"]
    assert adapter._state_store.load_thread("thread-1") == before


def test_subscribe_rejects_mismatched_invocation_before_observing(tmp_path):
    upstream = ObserveOnlyClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a", client=upstream, state_dir=tmp_path)
    adapter._persist_thread(
        ThreadBinding(
            thread_id="thread-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            user_id="user-1",
            ros_invocation_id="inv-1",
            execution_id="exec-1",
            task_id="task-1",
            run_digests={"run-1": "digest-1"},
        )
    )
    with TestClient(create_app(adapter=adapter, auth_token="token")) as client:
        response = client.post(
            "/extensions/iac-code/v1/executions/exec-1/subscribe",
            headers={"Authorization": "Bearer token"},
            json={"threadId": "thread-1", "runId": "run-1", "rosInvocationId": "other"},
        )
    assert response.status_code == 409
    assert upstream.observed == []


def test_agui_handoff_materializes_verified_images_with_current_credentials():
    import base64
    import hashlib

    import pytest

    from iac_code.agui.handoff_routes import AguiHandoffRoutes

    image = b"handoff-image"
    encoded = base64.b64encode(image).decode()
    descriptor = {
        "filename": "diagram.png",
        "media_type": "image/png",
        "size_bytes": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
    }
    payload = {
        "receipt": {
            "cwd": "/workspace/session-1",
            "userId": "user-1",
            "mode": "pipeline",
            "guidanceId": "guide-1",
            "aguiIdentity": {"threadId": "thread-1", "executionId": "exec-1", "rosInvocationId": "inv-1"},
        },
        "pendingInput": {
            "query": "continue",
            "image_attachments": {"items": [descriptor]},
            "image_parts": [{"bytes_base64": encoded}],
        },
        "inputDigest": "d" * 64,
        "target": {"sandboxId": "target", "activationId": "a", "leaseId": "l", "runId": "run-1"},
        "callerMetadata": {"iac_code": {"alibaba_cloud_access_key_id": "fake-current-caller"}},
    }
    routes = AguiHandoffRoutes(None, None)
    result = routes._pending_run_input(payload)
    assert result.thread_id == "thread-1" and result.run_id == "guide-1"
    assert result.messages[0].content[1].source.value == encoded
    props = result.forwarded_props["iacCode"]
    assert props["alibabaCloud"]["accessKeyId"] == "fake-current-caller"
    assert props["executionFence"]["owner"] == payload["target"]
    payload["pendingInput"]["image_parts"][0]["bytes_base64"] = "Y29ycnVwdA=="
    with pytest.raises(ValueError, match="Image checkpoint digest changed"):
        routes._pending_run_input(payload)
