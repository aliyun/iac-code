from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from scripts.repl.e2e.run_real_aliyun_contract_canary import (
    CONFIG_FILES,
    _aliyun_tool_uses,
    _child_env,
    _copy_runtime_config,
    _is_allowed_describe_vpcs,
    _is_business_vpc_body,
    main,
    parse_args,
)


def test_real_canary_requires_explicit_cloud_opt_in() -> None:
    with pytest.raises(SystemExit, match="--allow-real-cloud"):
        main([])


def test_real_canary_defaults_to_text_e2e_model() -> None:
    assert parse_args([]).model == "deepseek-v4-flash-0731"


def test_copy_runtime_config_requires_all_files_and_restricts_permissions(tmp_path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    for name in CONFIG_FILES:
        (source / name).write_text("fixture: true\n", encoding="utf-8")

    _copy_runtime_config(source, destination)

    for name in CONFIG_FILES:
        target = destination / name
        assert target.read_text(encoding="utf-8") == "fixture: true\n"
        # POSIX permission bits are not meaningful on Windows, which cannot
        # represent an owner-only 0o600 mode via os.chmod.
        if sys.platform != "win32":
            assert target.stat().st_mode & 0o777 == 0o600

    (source / CONFIG_FILES[0]).unlink()
    with pytest.raises(FileNotFoundError, match=CONFIG_FILES[0]):
        _copy_runtime_config(source, destination)


def test_real_canary_extracts_and_restricts_aliyun_tool_call(tmp_path) -> None:
    session = tmp_path / "session.jsonl"
    tool_input = {
        "product": "vpc",
        "version": "2016-04-28",
        "action": "DescribeVpcs",
        "params": {"PageSize": 10},
    }
    session.write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "read only"},
                    {"type": "tool_use", "name": "aliyun_api", "input": tool_input},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _aliyun_tool_uses(session) == [tool_input]
    assert _is_allowed_describe_vpcs(tool_input) is True
    assert _is_allowed_describe_vpcs({**tool_input, "params": {"PageSize": 20}}) is False
    assert _is_allowed_describe_vpcs({**tool_input, "action": "CreateVpc"}) is False


def test_real_canary_accepts_business_body_and_rejects_transport_envelope() -> None:
    assert _is_business_vpc_body({"RequestId": "request-1", "Vpcs": {"Vpc": []}}) is True
    assert _is_business_vpc_body({"body": {"RequestId": "request-1", "Vpcs": {"Vpc": []}}, "status": 200}) is False


def test_real_canary_child_env_removes_deterministic_fixtures(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_E2E_ALIYUN_TRANSPORT_FIXTURE", "1")
    monkeypatch.setenv("IAC_CODE_E2E_PROVIDER_CAPTURE", "/tmp/provider.jsonl")

    env = _child_env(config_dir=tmp_path, observe=SimpleNamespace(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "fixture"}))

    assert env["IAC_CODE_CONFIG_DIR"] == str(tmp_path)
    assert env["IAC_CODE_MODE"] == "normal"
    assert env["IAC_CODE_MODEL"] == "deepseek-v4-flash-0731"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "fixture"
    assert not any(key.startswith("IAC_CODE_E2E_") for key in env)
