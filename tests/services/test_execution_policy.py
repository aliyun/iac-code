"""Product policy extension contracts, independent of cloud or E2E state."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from iac_code.services import execution_policy as policy


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(policy, "_policy", None)
    return policy


def fake_policy():
    return SimpleNamespace(
        enabled=Mock(return_value=True),
        execution_instructions=Mock(return_value="host instructions"),
        filter_tools=Mock(side_effect=lambda tools: tools[:1]),
        input_schema=Mock(side_effect=lambda tool, schema: {**schema, "host": tool}),
        deferred_call=Mock(return_value="deferred by host"),
        require_runtime=Mock(return_value="runtime required"),
        check=Mock(return_value="denied by host"),
        reserve_when_ready=AsyncMock(return_value="opaque-ticket"),
        finish=Mock(),
        on_web_startup=Mock(),
    )


@pytest.mark.asyncio
async def test_unconfigured_host_preserves_product_behavior(host, monkeypatch):
    # Environment variables alone never import or install a policy.
    monkeypatch.setenv("IAC_CODE_TEST_EXECUTION_GATE", "/not/a/real/journal")
    monkeypatch.setenv("IAC_CODE_EXECUTION_POLICY", "not.a.real.module")
    tools, schema = [object(), object()], {"type": "object"}
    assert host.enabled() is False
    assert host.execution_instructions("session", "cwd") == ""
    assert host.filter_tools(tools) is tools
    assert host.input_schema("tool", schema) is schema
    assert host.deferred_call("tool", 1) is None
    assert host.require_runtime(False) is None
    assert host.check("tool", {}, "session", cwd="cwd") is None
    assert await host.reserve_when_ready({}, "session", object(), read_only=False) is None
    host.finish(None, {}, True)
    host.on_web_startup()


@pytest.mark.asyncio
async def test_explicit_host_policy_receives_exact_arguments(host):
    extension = fake_policy()
    host.install_policy(extension)
    tools, schema, credential = [object(), object()], {"type": "object"}, object()
    assert host.enabled()
    assert host.execution_instructions("session", "cwd") == "host instructions"
    assert host.filter_tools(tools) == tools[:1]
    assert host.input_schema("tool", schema) == {**schema, "host": "tool"}
    assert host.deferred_call("tool", 2) == "deferred by host"
    assert host.require_runtime(False) == "runtime required"
    assert host.check("tool", {"key": "value"}, "session", cwd="cwd") == "denied by host"
    extension.check.assert_called_once_with("tool", {"key": "value"}, "session", cwd="cwd")
    ticket = await host.reserve_when_ready(
        {"key": "value"}, "session", credential, read_only=True, cwd="cwd", tool_use_id="call"
    )
    extension.reserve_when_ready.assert_awaited_once_with(
        {"key": "value"}, "session", credential, read_only=True, cwd="cwd", tool_use_id="call"
    )
    host.finish(ticket, {"body": "actual"}, False, status=403, headers={"x-request": "actual"})
    extension.finish.assert_called_once_with(
        ticket, {"body": "actual"}, False, status=403, headers={"x-request": "actual"}
    )
    host.on_web_startup()
    extension.on_web_startup.assert_called_once_with()
    assert schema == {"type": "object"}


def test_policy_cannot_be_replaced_during_execution(host):
    extension = fake_policy()
    host.install_policy(extension)
    host.install_policy(extension)
    with pytest.raises(RuntimeError, match="already installed"):
        host.install_policy(fake_policy())


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("check", ("tool", {}, "session")),
        ("execution_instructions", ("session", "cwd")),
        ("filter_tools", ([],)),
        ("input_schema", ("tool", {})),
        ("deferred_call", ("tool", 1)),
        ("require_runtime", (False,)),
        ("finish", ("ticket", {}, True)),
        ("on_web_startup", ()),
    ],
)
def test_policy_failures_never_fall_back_to_allow(host, method, arguments):
    extension = fake_policy()
    getattr(extension, method).side_effect = policy.ExecutionPolicyDeniedError("safe denial")
    host.install_policy(extension)
    with pytest.raises(policy.ExecutionPolicyDeniedError, match="safe denial"):
        getattr(host, method)(*arguments)


@pytest.mark.asyncio
async def test_transport_policy_failure_never_returns_a_ticket(host):
    extension = fake_policy()
    extension.reserve_when_ready.side_effect = policy.ExecutionPolicyDeniedError("safe denial")
    host.install_policy(extension)
    with pytest.raises(policy.ExecutionPolicyDeniedError, match="safe denial"):
        await host.reserve_when_ready({}, "session", object(), read_only=False)


def test_product_imports_neither_load_e2e_nor_install_a_policy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from iac_code.services import execution_policy; "
                "import iac_code.agent.agent_loop; import iac_code.web.server; "
                "assert not execution_policy.enabled(); "
                "assert not any(name.startswith('scripts.web.e2e') for name in sys.modules)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_product_package_contains_no_resource_link_test_gate():
    package = Path(__file__).resolve().parents[2] / "src/iac_code"
    assert not (package / "services/test_execution_gate.py").exists()
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "scripts.web.e2e" not in text, path
        assert "IAC_CODE_TEST_EXECUTION_GATE" not in text, path
