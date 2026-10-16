"""Real file tools and public A2A artifact metadata survive editing and restart."""

import hashlib
from urllib.parse import urlsplit

import pytest

from tests.a2a.test_permission_handoff_process_e2e import assert_success
from tests.a2a.test_permission_prompt_e2e import Server

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def artifact_update(events):
    updates = [event["result"]["artifactUpdate"] for event in events if "artifactUpdate" in event.get("result", {})]
    assert len(updates) == 1, events
    return updates[0]


def assert_artifact(root, artifact, expected):
    metadata = artifact["metadata"]
    assert artifact["name"] == "template.yaml"
    assert metadata["byteSize"] == len(expected)
    assert metadata["sha256"] == hashlib.sha256(expected).hexdigest()
    reference = urlsplit(metadata["uri"])
    assert reference.scheme == "iac-code-artifact"
    assert reference.netloc == artifact["artifactId"]
    stored = root / "a2a/artifacts" / reference.netloc / "template.yaml"
    assert stored.read_bytes() == expected


def test_edited_template_artifact_content_and_metadata_survive_restart(tmp_path):
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path, artifact_mode=True)
    try:
        server.start()
        created = server.send("create template")
        assert_success(created, "completed approved batch")
        initial = artifact_update(created)
        old_bytes = (tmp_path / "workspace/template.yaml").read_bytes()
        assert_artifact(tmp_path, initial["artifact"], old_bytes)

        edited = server.send("edit template")
        assert_success(edited, "completed approved batch")
        latest = artifact_update(edited)
        new_bytes = (tmp_path / "workspace/template.yaml").read_bytes()
        assert b"Description: updated template content" in new_bytes
        assert new_bytes != old_bytes
        assert_artifact(tmp_path, latest["artifact"], new_bytes)
        assert latest["artifact"]["metadata"]["sha256"] != initial["artifact"]["metadata"]["sha256"]
        assert latest["artifact"]["metadata"]["byteSize"] != initial["artifact"]["metadata"]["byteSize"]

        for restart in (False, True):
            if restart:
                server.stop()
                server.start()
            response = server.client.post(
                "/",
                headers={"A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": "get-edited-task",
                    "method": "GetTask",
                    "params": {"id": latest["taskId"]},
                },
            )
            response.raise_for_status()
            body = response.json()
            assert "error" not in body, body
            task = body["result"].get("task", body["result"])
            assert "artifacts" in task, (restart, task)
            artifacts = task["artifacts"]
            assert len(artifacts) == 1
            assert artifacts[0] == latest["artifact"]
            assert_artifact(tmp_path, artifacts[0], new_bytes)
    finally:
        server.stop()
        server.client.close()
