from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.ci.ecs_dryrun_guard import explicit_ecs_dry_run, install_guard, wire_kind


@pytest.mark.parametrize("flag,expected", [(True, True), ("true", True), (False, False), (None, False),
                                            (1, False), ("false", False)])
def test_permission_requires_explicit_ecs_runinstances_dryrun(flag, expected):
    call = {"name": "aliyun_api", "input": {"product": "ecs", "action": "RunInstances", "params": {"DryRun": flag}}}
    assert explicit_ecs_dry_run(call) is expected
    call["input"]["action"] = "CreateInstance"
    assert not explicit_ecs_dry_run(call)


@pytest.mark.parametrize("product,action,query,body,content_type,expected", [
    ("Ecs", "RunInstances", (("DryRun", "true"),), None, "", "ecs_dry_run"),
    ("Ecs", "RunInstances", (), b"DryRun=true&Amount=1", "application/x-www-form-urlencoded", "ecs_dry_run"),
    ("Ecs", "RunInstances", (), b'{"DryRun":true}', "application/json", "ecs_dry_run"),
    ("Ecs", "RunInstances", (), None, "", "blocked"),
    ("Ecs", "RunInstances", (("DryRun", "false"),), None, "", "blocked"),
    ("Ecs", "RunInstances", (("DryRun", "true"),), b"DryRun=false", "application/x-www-form-urlencoded", "blocked"),
    ("Ecs", "RunInstances", (("DryRun", "true"), ("DryRun", "true")), None, "", "blocked"),
    ("ros", "RunInstances", (("DryRun", "true"),), None, "", "blocked"),
    ("Ecs", "CreateInstance", (("DryRun", "true"),), None, "", "blocked"),
    ("Ecs", "DeleteInstances", (), None, "", "blocked"),
    ("Ecs", "DescribeImages", (), None, "", "read"),
])
def test_wire_guard_checks_final_query_and_body(product, action, query, body, content_type, expected):
    assert wire_kind(product, action, query, body, content_type) == expected


@pytest.mark.asyncio
async def test_guard_blocks_before_transport_and_preserves_allowed_response(monkeypatch, tmp_path):
    from iac_code.tools.cloud.aliyun.acs3_transport import PreparedTransportCall

    reached = []
    response = object()

    async def dispatch(self, *, budget):
        reached.append(self)
        return response

    monkeypatch.setattr(PreparedTransportCall, "execute", dispatch)
    audit = tmp_path / "dispatch.jsonl"
    install_guard(audit)
    call = SimpleNamespace(contract=SimpleNamespace(product="Ecs", action="RunInstances"),
                           request=SimpleNamespace(canonical_query=(), body=None, headers={}),
                           context=SimpleNamespace(tool_use_id="test-call"))
    with pytest.raises(RuntimeError, match="dispatch blocked"):
        await PreparedTransportCall.execute(call, budget=None)
    assert reached == []
    call.request.canonical_query = (("DryRun", "true"),)
    assert await PreparedTransportCall.execute(call, budget=None) is response
    assert reached == [call]
    assert [json.loads(line)["event"] for line in audit.read_text(encoding="utf-8").splitlines()] == [
        "guard_installed", "blocked", "dispatch",
    ]
