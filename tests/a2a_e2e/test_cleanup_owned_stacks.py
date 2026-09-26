"""Ownership checks for CI cleanup of real A2A recovery stacks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.a2a.e2e.cleanup_owned_stacks import cleanup_owned_stacks


def _manifest(run_dir: Path, names: list[str]) -> None:
    (run_dir / "owned-stacks.json").write_text(
        json.dumps({"runId": "123456789abc", "stackNames": names, "regionId": "cn-hangzhou"}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "names",
    [[], ["unrelated-stack"], ["iac-e2e-123456789abd-main"], ["iac-e2e-123456789abc-main", 1]],
)
def test_cleanup_rejects_manifest_without_run_ownership(tmp_path: Path, names: list[str]) -> None:
    _manifest(tmp_path, names)
    with pytest.raises(ValueError, match="ownership"):
        cleanup_owned_stacks(tmp_path)


def test_cleanup_deletes_only_exact_named_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    name = "iac-e2e-123456789abc-main"
    _manifest(tmp_path, [name])
    deleted: list[str] = []

    class Client:
        def list_stacks(self, request):
            assert request.stack_name == [name]
            return SimpleNamespace(body=SimpleNamespace(stacks=[
                SimpleNamespace(stack_name=name, stack_id="owned-id"),
                SimpleNamespace(stack_name="some-other-stack", stack_id="foreign-id"),
            ]))

        def get_stack(self, request):
            assert request.stack_id == "owned-id"
            status = "DELETE_COMPLETE" if deleted else "CREATE_COMPLETE"
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {"StackName": name, "Status": status}))

        def delete_stack(self, request):
            deleted.append(request.stack_id)

    monkeypatch.setattr(CloudCredentials, "get_provider", lambda _self, _name: SimpleNamespace(region_id="cn-hangzhou"))
    monkeypatch.setattr(RosClientFactory, "create", lambda _credential, _region: Client())
    monkeypatch.setattr("scripts.a2a.e2e.cleanup_owned_stacks.time.sleep", lambda _delay: None)

    result = cleanup_owned_stacks(tmp_path, timeout=5)

    assert result["status"] == "completed"
    assert deleted == ["owned-id"]
    assert result["deletedStackIds"] == ["owned-id"]


def test_cleanup_refuses_listed_stack_with_mismatched_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    name = "iac-e2e-123456789abc-main"
    _manifest(tmp_path, [name])

    class Client:
        def list_stacks(self, _request):
            return SimpleNamespace(body=SimpleNamespace(stacks=[SimpleNamespace(stack_name=name, stack_id="id")]))

        def get_stack(self, _request):
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                "StackName": "some-other-stack", "Status": "CREATE_COMPLETE",
            }))

        def delete_stack(self, _request):
            pytest.fail("foreign stack must not be deleted")

    monkeypatch.setattr(CloudCredentials, "get_provider", lambda _self, _name: SimpleNamespace(region_id="cn-hangzhou"))
    monkeypatch.setattr(RosClientFactory, "create", lambda _credential, _region: Client())

    result = cleanup_owned_stacks(tmp_path, timeout=5)

    assert result["status"] == "failed"
    assert result["remainingStackIds"] == ["id"]
