"""HTTP model wire acceptance after a new prompt cancels a permission batch."""

import json

import pytest

from scripts.a2a.e2e.permission_wait.protocol_fixture import ModelServer, validate_canceled_request
from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_real_provider_sends_canceled_results_before_new_prompt(tmp_path, protocol):
    (tmp_path / "workspace").mkdir()
    with ModelServer(protocol) as model:
        server = Server(tmp_path, protocol=protocol, model_url=model.url)
        try:
            server.start()
            first = _permission(server.send("original"))
            second = _permission(server.send(_reply(first)))
            assert first["toolUseId"] == "tool-1" and second["toolUseId"] == "tool-2"
            assert len(model.requests) == 1
            events = server.send("new prompt")
            assert not any("error" in event for event in events), events
            assert "handled new prompt" in json.dumps(events)
            assert not (tmp_path / "executions.jsonl").exists()
            assert not model.errors, model.errors
            assert len(model.requests) == 2
            validate_canceled_request(protocol, model.requests[-1])
            server.stop()
            server.start()
            for permission in (first, second):
                server.send(_reply(permission), task_id=permission["requestTaskId"])
            assert not model.errors, model.errors
            assert len(model.requests) == 2
            assert not (tmp_path / "executions.jsonl").exists()
        finally:
            server.stop()
            server.client.close()
