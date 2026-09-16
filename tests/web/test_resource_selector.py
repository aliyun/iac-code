from __future__ import annotations

from starlette.testclient import TestClient

from iac_code.resource_selector.profiles import PROFILE_HASH
from iac_code.resource_selector.query import ResourceSelectorQueryService
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


def payload(input_id: str = "resource-test") -> dict:
    return {
        "toolUseId": "tool-1",
        "inputId": input_id,
        "question": "请选择 ECS 实例",
        "selector": {
            "id": "ecs.instance",
            "associationProperty": "ALIYUN::ECS::Instance::InstanceId",
            "outputKind": "resource_id",
            "associationPropertyMetadata": {"RegionId": "cn-hangzhou"},
            "source": None,
            "profileHash": PROFILE_HASH,
        },
    }


def manager_and_session(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=cwd)
    return manager, manager.create_session(cwd=str(cwd))


def test_answer_is_explicit_single_value_idempotent_and_conflict_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager, session = manager_and_session(tmp_path)
    request_id = manager.add_resource_selection_request(session, payload())

    async def caller(_product, _action, _region_id, _params):
        return {"Instances": {"Instance": [{"InstanceId": "i-test123", "InstanceName": "app-server"}]}}

    query_service = ResourceSelectorQueryService(caller)
    with TestClient(
        create_app(session_manager=manager, resource_selector_query_service=query_service)
    ) as client:
        body = {
            "sessionId": session.session_id,
            "inputId": "resource-test",
            "selectorId": "ecs.instance",
            "value": "i-test123",
            "label": "app-server",
        }
        observed = client.post(
            "/api/resource-selector/query",
            json={
                "requestId": request_id,
                "sessionId": session.session_id,
                "inputId": "resource-test",
                "operationKey": "ecs.instance.list",
                "params": {"MaxResults": 20},
            },
        )
        assert observed.status_code == 200
        assert client.post(f"/api/resource-selections/{request_id}/answer", json=body).status_code == 200
        replay = client.post(f"/api/resource-selections/{request_id}/answer", json=body)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        conflict = client.post(
            f"/api/resource-selections/{request_id}/answer",
            json={**body, "value": "i-other123"},
        )
        assert conflict.status_code == 409
        assert session.resolved_resource_selections[request_id]["value"] == "i-test123"


def test_cancel_returns_structured_result_and_query_uses_pending_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager, session = manager_and_session(tmp_path)
    request_id = manager.add_resource_selection_request(session, payload())

    class QueryService:
        def __init__(self):
            self.discarded = False

        async def query(self, **kwargs):
            assert kwargs["operation_key"] == "ecs.instance.list"
            assert kwargs["dynamic_parameters"] == {"MaxResults": 20}
            assert kwargs["pending_payload"]["selector"]["profileHash"] == PROFILE_HASH
            return {"Instances": {"Instance": []}}

        def options_empty(self, **_kwargs):
            return True

        def discard(self, **_kwargs):
            self.discarded = True

    query_service = QueryService()
    with TestClient(create_app(session_manager=manager, resource_selector_query_service=query_service)) as client:
        queried = client.post(
            "/api/resource-selector/query",
            json={
                "requestId": request_id,
                "sessionId": session.session_id,
                "inputId": "resource-test",
                "operationKey": "ecs.instance.list",
                "params": {"MaxResults": 20},
            },
        )
        assert queried.status_code == 200
        assert queried.json()["response"] == {"Instances": {"Instance": []}}

        canceled = client.post(
            f"/api/resource-selections/{request_id}/cancel",
            json={
                "sessionId": session.session_id,
                "inputId": "resource-test",
                "selectorId": "ecs.instance",
            },
        )
        assert canceled.status_code == 200
        assert canceled.json()["optionsEmpty"] is True
        assert query_service.discarded is True
        assert session.resolved_resource_selections[request_id] == {
            "status": "canceled",
            "input_id": "resource-test",
            "selector_id": "ecs.instance",
            "options_empty": True,
        }


def test_pending_request_is_present_in_page_refresh_state(tmp_path) -> None:
    manager, session = manager_and_session(tmp_path)
    request_id = manager.add_resource_selection_request(session, payload())
    state = session.to_dict()
    assert state["pendingResourceSelectionCount"] == 1
    assert state["pendingResourceSelections"][0]["requestId"] == request_id
