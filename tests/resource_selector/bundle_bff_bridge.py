"""JSON-lines bridge used by the ORE bundle E2E to exercise the real Web BFF routes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from iac_code.resource_selector.profiles import PROFILE_HASH, QueryOperation, get_profile
from iac_code.resource_selector.query import ResourceSelectorQueryService
from iac_code.resource_selector.tools import selection_result
from iac_code.resource_selector.validation import normalize_metadata
from iac_code.types.stream_events import CloudResourceSelectionEvent
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager

ROOT = Path(__file__).resolve().parent
CASES = {
    item["selectorId"]: item
    for item in json.loads((ROOT / "e2e-cases.json").read_text(encoding="utf-8"))["cases"]
}
FIXTURES = json.loads((ROOT / "contract-fixtures.json").read_text(encoding="utf-8"))["fixtures"]


class Bridge:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="iac-code-resource-selector-e2e-")
        root = Path(self._temporary.name)
        os.environ["IAC_CODE_CONFIG_DIR"] = str(root / "config")
        project = root / "project"
        project.mkdir()
        self._manager = WebSessionManager(projects_dir=root / "sessions", cwd=project)
        self._session = self._manager.create_session(cwd=str(project))
        self._fixtures_by_signature: dict[tuple[str, str], tuple[QueryOperation, dict[str, Any]]] = {}
        self._query_service = ResourceSelectorQueryService(self._caller)
        self._client = TestClient(
            create_app(session_manager=self._manager, resource_selector_query_service=self._query_service)
        )
        self._client.__enter__()
        self._active: dict[str, Any] | None = None

    async def _caller(
        self,
        product: str,
        action: str,
        _region_id: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        operation, response = self._fixtures_by_signature[(product, action)]
        if operation.request_kind == "multiApi":
            return next(iter(response.values()))
        if self._active and self._active["selectorId"] == "ecs.instance" and self._active["scenario"]:
            instances = [
                {
                    "InstanceId": "i-test{:04d}".format(index),
                    "InstanceName": "test-ecs-{}".format(index),
                    "Status": "Running",
                }
                for index in range(1, 21)
            ]
            if params.get("NextToken"):
                instances = [{"InstanceId": "i-test0021", "InstanceName": "test-ecs-21", "Status": "Running"}]
            return {
                "Instances": {"Instance": instances},
                "NextToken": "" if params.get("NextToken") else "next-page-token",
                "TotalCount": 21,
            }
        if (
            self._active
            and self._active["selectorId"] in {"oos.parameter", "oos.secret_parameter"}
            and self._active["scenario"]
        ):
            prefix = "secret" if self._active["selectorId"] == "oos.secret_parameter" else "parameter"
            if params.get("Name"):
                names = [str(params["Name"])]
            elif params.get("NextToken"):
                names = ["{}-21".format(prefix)]
            else:
                names = ["{}-{:02d}".format(prefix, index) for index in range(1, 21)]
            return {
                "Parameters": [{"Name": name} for name in names],
                "NextToken": "" if params.get("NextToken") or params.get("Name") else "next-page-token",
                "MaxResults": len(names),
            }
        if (
            self._active
            and self._active["selectorId"] == "resource_manager.folder"
            and self._active["scenario"]
            and action == "ListAuthorizedFolders"
        ):
            return {
                "TotalCount": 2,
                "PageNumber": 1,
                "PageSize": 100,
                "Folders": {
                    "Folder": [
                        {
                            "FolderId": "fd-parent",
                            "FolderName": "parent",
                            "ResourceDirectoryPath": "rd-root/fd-parent",
                        },
                        {
                            "FolderId": "value-resource-manager-folder",
                            "FolderName": "target",
                            "ResourceDirectoryPath": "rd-root/fd-parent/value-resource-manager-folder",
                        },
                    ]
                },
            }
        if action == "ListFoldersForParent" and params.get("ParentFolderId") == "fd-test0001":
            response = dict(response)
            response["Folders"] = {"Folder": []}
            response["TotalCount"] = 0
        return response

    def setup(
        self,
        selector_id: str,
        *,
        scenario: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case = CASES[selector_id]
        profile = get_profile(selector_id)
        if profile is None or not profile.enabled:
            raise ValueError("selector_profile_mismatch")
        self._fixtures_by_signature = {}
        for request in case["expectedRequests"]:
            operation = next(item for item in profile.operations if item.key == request["operationKey"])
            response = FIXTURES[request["fixtureId"]]["response"]
            self._fixtures_by_signature[(operation.product, operation.action)] = (operation, response)
        input_id = "bundle-bff-{}".format(selector_id.replace(".", "-"))
        effective_metadata = {**case["metadata"], **(metadata or {})}
        validation = normalize_metadata(
            profile,
            effective_metadata,
            default_region_provider=lambda: "cn-hangzhou",
        )
        if not validation.valid:
            raise ValueError("selector_metadata_invalid")
        effective_metadata = validation.normalized
        payload = {
            "toolUseId": "tool-{}".format(selector_id),
            "inputId": input_id,
            "question": "请选择资源",
            "selector": {
                "id": profile.selector_id,
                "associationProperty": profile.association_property,
                "outputKind": case["expected"]["outputKind"],
                "associationPropertyMetadata": effective_metadata,
                "source": case["source"],
                "profileHash": PROFILE_HASH,
            },
        }
        request_id = self._manager.add_resource_selection_request(self._session, payload)
        self._active = {
            "requestId": request_id,
            "inputId": input_id,
            "selectorId": selector_id,
            "profile": profile,
            "case": case,
            "metadata": effective_metadata,
            "scenario": scenario,
        }
        return {
            "requestId": request_id,
            "inputId": input_id,
            "selectorId": selector_id,
            "sessionId": self._session.session_id,
        }

    def query(self, operation_key: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._active is None:
            raise ValueError("no_active_case")
        response = self._client.post(
            "/api/resource-selector/query",
            json={
                "requestId": self._active["requestId"],
                "inputId": self._active["inputId"],
                "sessionId": self._session.session_id,
                "operationKey": operation_key,
                "params": params,
            },
        )
        if response.status_code != 200:
            raise ValueError("BFF query failed {}: {}".format(response.status_code, response.text))
        return response.json()["response"]

    def answer(self, value: str, label: str) -> dict[str, Any]:
        if self._active is None:
            raise ValueError("no_active_case")
        request_id = self._active["requestId"]
        response = self._client.post(
            "/api/resource-selections/{}/answer".format(request_id),
            json={
                "sessionId": self._session.session_id,
                "inputId": self._active["inputId"],
                "selectorId": self._active["selectorId"],
                "value": value,
                "label": label,
            },
        )
        if response.status_code != 200:
            raise ValueError("BFF answer failed {}: {}".format(response.status_code, response.text))
        resolved = self._session.resolved_resource_selections[request_id]
        profile = self._active["profile"]
        case = self._active["case"]
        event = CloudResourceSelectionEvent(
            tool_use_id="tool-{}".format(profile.selector_id),
            input_id=self._active["inputId"],
            question="请选择资源",
            selector_id=profile.selector_id,
            association_property=profile.association_property,
            output_kind=case["expected"]["outputKind"],
            association_property_metadata=case["metadata"],
            source=case["source"],
            profile_hash=PROFILE_HASH,
        )
        tool_result = json.loads(selection_result(profile, event, resolved).content)
        self._active = None
        return {"response": response.json(), "resolved": resolved, "toolResult": tool_result}

    def cancel(self) -> dict[str, Any]:
        if self._active is None:
            return {"status": "idle"}
        request_id = self._active["requestId"]
        response = self._client.post(
            "/api/resource-selections/{}/cancel".format(request_id),
            json={
                "sessionId": self._session.session_id,
                "inputId": self._active["inputId"],
                "selectorId": self._active["selectorId"],
            },
        )
        if response.status_code != 200:
            raise ValueError("BFF cancel failed {}: {}".format(response.status_code, response.text))
        resolved = self._session.resolved_resource_selections[request_id]
        self._active = None
        return resolved

    def close(self) -> None:
        if self._active is not None:
            self.cancel()
        self._client.__exit__(None, None, None)
        self._temporary.cleanup()


def main() -> None:
    bridge = Bridge()
    try:
        for line in sys.stdin:
            message: dict[str, Any] = json.loads(line)
            request_id = message.get("id")
            try:
                command = message.get("command")
                if command == "setup":
                    result = bridge.setup(
                        str(message["selectorId"]),
                        scenario=bool(message.get("scenario")),
                        metadata=dict(message.get("metadata") or {}),
                    )
                elif command == "query":
                    result = bridge.query(str(message["operationKey"]), dict(message.get("params") or {}))
                elif command == "answer":
                    result = bridge.answer(str(message["value"]), str(message.get("label") or message["value"]))
                elif command == "cancel":
                    result = bridge.cancel()
                elif command == "close":
                    result = {"status": "closed"}
                    print(json.dumps({"id": request_id, "ok": True, "result": result}), flush=True)
                    break
                else:
                    raise ValueError("unknown_command")
                print(json.dumps({"id": request_id, "ok": True, "result": result}), flush=True)
            except Exception as exc:  # noqa: BLE001 - transport errors must cross the process boundary
                print(json.dumps({"id": request_id, "ok": False, "error": str(exc)}), flush=True)
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
