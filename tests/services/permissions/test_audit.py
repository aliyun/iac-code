import json
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from iac_code.services.permissions.audit import (
    PermissionAuditRecord,
    build_display_tool_input,
    build_input_summary,
    build_prompt_tool_input,
    build_redacted_tool_input,
    emit_permission_audit,
    emit_permission_boundary_audit,
    fingerprint_text,
    is_permission_audit_non_read_only,
    sanitize_free_text,
)
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings
from iac_code.utils.project_paths import sanitize_path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _session_audit_log_path(config_dir: Path, cwd: str, session_id: str) -> Path:
    return config_dir / "projects" / sanitize_path(cwd) / session_id / "permission-audit.jsonl"


def _audit_log_path_for_record(config_dir: Path, record: PermissionAuditRecord) -> Path:
    cwd = record.cwd.strip() or os.getcwd()
    session_id = record.session_id.strip() or "unknown-session"
    return _session_audit_log_path(config_dir, cwd, session_id)


def _read_audit_rows(config_dir: Path, record: PermissionAuditRecord) -> list[dict]:
    return _read_jsonl(_audit_log_path_for_record(config_dir, record))


def _has_truncated_object(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "object" and value.get("truncated") is True:
            return True
        return any(_has_truncated_object(child) for child in value.values())
    return False


def test_fingerprint_is_stable_and_short() -> None:
    assert fingerprint_text("secret-value") == fingerprint_text("secret-value")
    assert fingerprint_text("secret-value").startswith("sha256:")


def test_generic_summary_excludes_raw_values() -> None:
    summary = build_input_summary("bash", {"command": "rm /secret", "nested": {"token": "abc"}})
    assert summary["tool_name"] == "bash"
    assert "command" in summary["fields"]
    assert "rm /secret" not in json.dumps(summary)
    assert "abc" not in json.dumps(summary)


def test_generic_summary_sanitizes_arbitrary_field_names() -> None:
    summary = build_input_summary(
        "bash",
        {
            "token=secret-token": "value",
            "nested": {"apiKey=api-secret": "value"},
        },
    )
    serialized = json.dumps(summary)
    assert "token=secret-token" not in serialized
    assert "apiKey=api-secret" not in serialized
    assert fingerprint_text("token=secret-token") in serialized
    assert fingerprint_text("apiKey=api-secret") in serialized


def test_generic_summary_fingerprints_business_field_names() -> None:
    summary = build_input_summary(
        "bash",
        {
            "command": "git status",
            "customerEmail": "alice@example.com",
            "customer-prod-123": "tenant-id",
        },
    )

    serialized = json.dumps(summary)
    assert "command" in summary["fields"]
    assert "customerEmail" not in serialized
    assert "customer-prod-123" not in serialized
    assert fingerprint_text("customerEmail") in summary["fields"]
    assert fingerprint_text("customer-prod-123") in summary["fields"]


def test_aliyun_summary_fingerprints_unsafe_values() -> None:
    summary = build_input_summary(
        "aliyun_api",
        {
            "product": "ros*",
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
            "style": "ROA",
            "method": "POST",
            "pathname": "/clusters/c-secret-123/nodes",
            "params": {"StackName": "prod", "token=secret-token": "value"},
        },
    )
    serialized = json.dumps(summary)
    assert "c-secret-123" not in serialized
    assert "StackName" not in serialized
    assert fingerprint_text("StackName") in serialized
    assert "token=secret-token" not in serialized
    assert fingerprint_text("token=secret-token") in serialized
    assert "product_fingerprint" in serialized
    assert "api_fingerprint" not in serialized
    assert "ros*" not in serialized


def test_input_summary_truncates_deep_and_wide_shapes() -> None:
    nested: object = "leaf"
    for _ in range(100):
        nested = {"next": nested}
    wide = {f"field_{index}": index for index in range(100)}

    summary = build_input_summary("bash", {"nested": nested, "wide": wide})

    nested_key = fingerprint_text("nested")
    wide_key = fingerprint_text("wide")
    assert _has_truncated_object(summary["fields"][nested_key])
    assert summary["fields"][wide_key]["_truncated"] == {"type": "object", "truncated": True}
    assert "field_99" not in summary["fields"][wide_key]["fields"]


def test_aliyun_summary_uses_field_fingerprints_with_width_limit() -> None:
    params = {f"StackName{index}": index for index in range(100)}

    summary = build_input_summary("aliyun_api", {"params": params})

    assert summary["params_field_count"] == 100
    assert summary["params_fields_truncated"] is True
    assert len(summary["params_fields"]) == 64
    serialized = json.dumps(summary)
    assert "StackName0" not in serialized
    assert fingerprint_text("StackName0") in serialized
    assert "StackName99" not in serialized


def test_emit_permission_audit_preserves_field_shape_fingerprint_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-field-shapes",
        tool_name="aliyun_api",
        tool_use_id="tu-field-shapes",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        input_summary=build_input_summary("aliyun_api", {"params": {"StackName": "demo"}}),
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    [row] = _read_audit_rows(tmp_path, record)
    field_key = fingerprint_text("StackName")
    assert row["input_summary"]["params_fields"] == [field_key]
    assert field_key in row["input_summary"]["params_field_shapes"]


def test_emit_permission_audit_sanitizes_raw_field_shape_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-field-shapes",
        tool_name="aliyun_api",
        tool_use_id="tu-field-shapes",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        input_summary={
            "tool_name": "aliyun_api",
            "params_fields": ["StackName"],
            "params_field_shapes": {"StackName": {"type": "str"}},
        },
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    [row] = _read_audit_rows(tmp_path, record)
    field_key = fingerprint_text("StackName")
    serialized = json.dumps(row)
    assert "StackName" not in serialized
    assert row["input_summary"]["params_fields"] == [field_key]
    assert field_key in row["input_summary"]["params_field_shapes"]


def test_build_display_tool_input_truncates_deep_wide_and_cyclic_values() -> None:
    nested: object = "leaf"
    for _ in range(100):
        nested = {"next": nested}
    wide = {f"field_{index}": index for index in range(100)}
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    display = build_display_tool_input({"path": "main.tf", "nested": nested, "wide": wide, "cyclic": cyclic})

    assert display["path"] == "main.tf"
    assert _has_truncated_object(display["nested"])
    assert display["wide"]["_truncated"] == {"type": "object", "truncated": True}
    assert "field_99" not in display["wide"]
    assert _has_truncated_object(display["cyclic"])


def test_build_display_tool_input_preserves_priority_fields_in_wide_objects() -> None:
    tool_input = {f"field_{index}": index for index in range(100)}
    tool_input["command"] = "rm -rf /"

    display = build_display_tool_input(tool_input)

    assert display["command"] == "rm -rf /"
    assert display["_truncated"] == {"type": "object", "truncated": True}
    assert "field_99" not in display


def test_build_display_tool_input_marks_long_strings_with_suffix() -> None:
    command = ("echo safe && " * 40) + "rm -rf /"

    display = build_display_tool_input({"command": command})

    assert display["command"]["type"] == "str"
    assert display["command"]["length"] == len(command)
    assert display["command"]["truncated"] is True
    assert "prefix" in display["command"]
    assert "rm -rf /" in display["command"]["suffix"]


def test_build_prompt_tool_input_redacts_space_separated_secret_flags_and_preserves_paths() -> None:
    command = "cat /Users/alice/project/main.tf --token abc123value --password 'hunter2' --api-key sk-live-secret"

    prompt = build_prompt_tool_input({"command": command, "path": "/Users/alice/project/main.tf"})

    rendered = json.dumps(prompt, ensure_ascii=False)
    assert "/Users/alice/project/main.tf" in rendered
    assert "--token [REDACTED]" in rendered
    assert "--password [REDACTED]" in rendered
    assert "--api-key [REDACTED]" in rendered
    assert "abc123value" not in rendered
    assert "hunter2" not in rendered
    assert "sk-live-secret" not in rendered
    assert "[PATH]" not in rendered


def test_build_prompt_tool_input_redacts_env_secret_assignments_without_false_flag_matches() -> None:
    command = (
        "OPENAI_API_KEY=sk-openai AWS_SECRET_ACCESS_KEY='aws-secret' "
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET=aliyun-secret echo my-secret value /Users/alice/project/main.tf"
    )

    prompt = build_prompt_tool_input({"command": command, "path": "/Users/alice/project/main.tf"})

    rendered = json.dumps(prompt, ensure_ascii=False)
    assert "OPENAI_API_KEY=[REDACTED]" in rendered
    assert "AWS_SECRET_ACCESS_KEY=[REDACTED]" in rendered
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET=[REDACTED]" in rendered
    assert "my-secret value" in rendered
    assert "/Users/alice/project/main.tf" in rendered
    for forbidden in ("sk-openai", "aws-secret", "aliyun-secret", "[PATH]"):
        assert forbidden not in rendered


def test_build_prompt_tool_input_redacts_long_secret_assignments_before_truncating() -> None:
    secret = "sk-" + ("x" * 260) + "tail-secret"
    command = f"OPENAI_API_KEY={secret} echo done /Users/alice/project/main.tf"

    prompt = build_prompt_tool_input({"command": command, "path": "/Users/alice/project/main.tf"})

    rendered = json.dumps(prompt, ensure_ascii=False)
    assert "OPENAI_API_KEY=[REDACTED]" in rendered
    assert "tail-secret" not in rendered
    assert secret not in rendered
    assert "/Users/alice/project/main.tf" in rendered
    assert "[PATH]" not in rendered


def test_build_prompt_tool_input_redacts_json_and_escaped_quote_secret_values() -> None:
    command = (
        'curl -d \'{"apiKey":"sk-json-secret"}\' '
        'OPENAI_API_KEY="abc\\"escaped-tail-secret" '
        "/Users/alice/project/main.tf"
    )

    prompt = build_prompt_tool_input({"command": command, "path": "/Users/alice/project/main.tf"})

    rendered = json.dumps(prompt, ensure_ascii=False)
    assert "apiKey" in rendered
    assert "OPENAI_API_KEY=[REDACTED]" in rendered
    assert "/Users/alice/project/main.tf" in rendered
    for forbidden in ("sk-json-secret", "escaped-tail-secret", "[PATH]"):
        assert forbidden not in rendered


def test_emit_permission_audit_truncates_deep_input_summary_before_serializing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    nested: object = "leaf"
    for _ in range(1500):
        nested = {"next": nested}
    record = PermissionAuditRecord(
        session_id="s-summary-depth",
        tool_name="bash",
        tool_use_id="tu-summary-depth",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        input_summary={"tool_name": "bash", "fields": {"payload": nested}},
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024 * 1024, max_files=2))

    [row] = _read_audit_rows(tmp_path, record)
    assert _has_truncated_object(row["input_summary"])


def test_boundary_audit_helper_emits_prompt_record(monkeypatch) -> None:
    records = []
    settings = PermissionAuditSettings(include_tool_input=True, max_file_bytes=123, max_files=2)
    event = Mock(
        tool_name="write_file",
        tool_input={"path": "main.tf", "content": "resource {}", "access_key_secret": "secret-value"},
        tool_use_id="tool1",
        audit_context={
            "session_id": "session-boundary",
            "settings": settings,
            "metadata": PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={"action": "CreateFile"},
            ),
        },
    )

    def fake_emit(record, settings=None):
        records.append((record, settings))

    monkeypatch.setattr("iac_code.services.permissions.audit.emit_permission_audit", fake_emit)

    emitted = emit_permission_boundary_audit(
        event,
        decision="allow",
        scope="session_rule",
        source="acp_prompt",
        reason_detail="allow_rule:path:main.tf",
        rule="write_file(path:main.tf)",
    )

    assert emitted is True
    [(record, settings_seen)] = records
    assert settings_seen is settings
    assert record.session_id == "session-boundary"
    assert record.tool_name == "write_file"
    assert record.tool_use_id == "tool1"
    assert record.source == "acp_prompt"
    assert record.scope == "session_rule"
    assert record.decision == "allow"
    assert record.reason_detail == "allow_rule:path:main.tf"
    assert record.rule == "write_file(path:main.tf)"
    assert record.operation == {"action": "CreateFile", "is_read_only": False}
    assert record.input_summary["fields"]["path"] == {"type": "str"}
    assert record.tool_input_redacted == {
        "path": {"type": "str", "length": 7, "fingerprint": fingerprint_text("main.tf")},
        "content": {"type": "str", "length": 11, "fingerprint": fingerprint_text("resource {}")},
        fingerprint_text("access_key_secret"): {"redacted": True},
    }


def test_boundary_audit_helper_emits_read_only_denial(monkeypatch) -> None:
    records = []
    event = Mock(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "GetStack"},
        tool_use_id="tool1",
        audit_context={
            "metadata": PermissionAuditMetadata(scope="read_only", source="permission_pipeline", is_read_only=True)
        },
    )

    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_audit",
        lambda record, settings=None: records.append(record),
    )

    assert is_permission_audit_non_read_only(event) is False
    assert emit_permission_boundary_audit(event, decision="deny", scope="tool_cache", source="repl_tool_cache") is True
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].scope == "tool_cache"
    assert records[0].operation == {"is_read_only": True}


def test_sanitize_free_text_redacts_and_caps() -> None:
    text = sanitize_free_text(
        "accessKeyId=ak-secret x-api-key:api-secret token=secret-token "
        "Signature=signature-secret Authorization: Bearer bearer-secret " + ("x" * 300),
        max_chars=160,
    )
    assert "ak-secret" not in text
    assert "api-secret" not in text
    assert "secret-token" not in text
    assert "signature-secret" not in text
    assert "bearer-secret" not in text
    assert len(text) <= 160


def test_emit_permission_audit_writes_jsonl_and_telemetry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    telemetry = Mock()
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", telemetry)
    cwd = "/home/workspace/context-1"
    record = PermissionAuditRecord(
        session_id="s1",
        cwd=cwd,
        tool_name="aliyun_api",
        tool_use_id="tu1",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        rule_source="user_settings",
        reason_type="rule",
        rule="ros:CreateStack",
        operation={"product": "ros", "action": "CreateStack", "region": "cn-hangzhou", "is_read_only": False},
        input_summary={"tool_name": "aliyun_api"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    log_path = _session_audit_log_path(tmp_path, cwd, "s1")
    rows = _read_jsonl(log_path)
    assert rows[0]["decision"] == "allow"
    assert rows[0]["rule_source"] == "user_settings"
    assert not (tmp_path / "logs" / "permission-audit.jsonl").exists()
    telemetry.assert_called_once()
    assert telemetry.call_args.args[0] == "iac.tool.permission.granted"


def test_emit_permission_audit_requests_durable_jsonl_append(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    append_calls = []

    def fake_append(path, records, **kwargs):
        append_calls.append((path, list(records), kwargs))

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fake_append)
    record = PermissionAuditRecord(
        session_id="s-durable",
        tool_name="aliyun_api",
        tool_use_id="tu-durable",
        decision="allow",
        scope="once",
        source="repl_prompt",
        operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
        input_summary={"tool_name": "aliyun_api"},
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2)) is True

    [(_path, _records, kwargs)] = append_calls
    assert kwargs["durable"] is True
    assert kwargs["create_mode"] == 0o600


def test_emit_permission_audit_skips_allow_telemetry_when_jsonl_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    telemetry = Mock()
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", telemetry)

    def fail_append(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fail_append)
    record = PermissionAuditRecord(
        session_id="s-fail",
        tool_name="bash",
        tool_use_id="tu-fail",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2)) is False
    telemetry.assert_not_called()


def test_emit_permission_audit_clamps_excessive_max_files_before_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "iac_code.services.permissions.audit._log_path",
        lambda _record: tmp_path / "permission-audit.jsonl",
    )
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    captured: dict[str, int] = {}

    def fake_append(path, records, **kwargs):
        captured["append_max_files"] = kwargs["max_files"]

    def fake_private(path, *, max_files):
        captured["private_max_files"] = max_files

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fake_append)
    monkeypatch.setattr("iac_code.services.permissions.audit._ensure_audit_log_files_private", fake_private)
    record = PermissionAuditRecord(
        session_id="s-clamp",
        tool_name="bash",
        tool_use_id="tu-clamp",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_files=100000000)) is True
    assert captured == {"append_max_files": 100, "private_max_files": 100}


def test_emit_permission_audit_restricts_jsonl_file_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        input_summary={"tool_name": "aliyun_api"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    if os.name != "nt":
        mode = stat.S_IMODE(_audit_log_path_for_record(tmp_path, record).stat().st_mode)
        assert mode == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_emit_permission_audit_allows_append_when_chmod_cannot_restrict_session_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    monkeypatch.setattr("iac_code.services.permissions.audit.ensure_private_file", lambda path: path)
    cwd = "/home/workspace/context-chmod"

    def fake_append(path, records, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(records)[0]) + "\n", encoding="utf-8")
        path.chmod(0o666)

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fake_append)
    record = PermissionAuditRecord(
        session_id="s-broad-session",
        cwd=cwd,
        tool_name="bash",
        tool_use_id="tu-broad-session",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2)) is True
    [row] = _read_jsonl(_session_audit_log_path(tmp_path, cwd, "s-broad-session"))
    assert row["decision"] == "allow"
    assert not (tmp_path / "logs" / "permission-audit.jsonl").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_emit_permission_audit_succeeds_when_jsonl_permissions_remain_too_broad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "iac_code.services.permissions.audit._log_path",
        lambda _record: tmp_path / "permission-audit.jsonl",
    )
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    monkeypatch.setattr("iac_code.services.permissions.audit.ensure_private_file", lambda path: path)

    def fake_append(path, records, **kwargs):
        path.write_text(json.dumps(list(records)[0]) + "\n", encoding="utf-8")
        path.chmod(0o666)

    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", fake_append)
    record = PermissionAuditRecord(
        session_id="s-broad",
        tool_name="bash",
        tool_use_id="tu-broad",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2)) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_emit_permission_audit_appends_existing_broad_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "permission-audit.jsonl"
    log_path.write_text("", encoding="utf-8")
    log_path.chmod(0o666)
    monkeypatch.setattr("iac_code.services.permissions.audit._log_path", lambda _record: log_path)
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    monkeypatch.setattr("iac_code.services.permissions.audit.ensure_private_file", lambda path: path)
    append = Mock(
        side_effect=lambda path, records, **kwargs: path.write_text(
            json.dumps(list(records)[0]) + "\n",
            encoding="utf-8",
        )
    )
    monkeypatch.setattr("iac_code.services.permissions.audit.append_jsonl_rotating_locked", append)
    record = PermissionAuditRecord(
        session_id="s-existing-broad",
        tool_name="bash",
        tool_use_id="tu-existing-broad",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
    )

    assert emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2)) is True
    append.assert_called_once()


def test_emit_permission_audit_omits_unsafe_rule_text_from_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="bash",
        tool_use_id="tu-private",
        decision="allow",
        scope="session_rule",
        source="permission_pipeline",
        rule_source="session",
        reason_type="rule",
        reason_detail="matched allow rule(s): bash(echo secret-value)",
        rule="bash(echo secret-value)",
        operation={"is_read_only": False},
        input_summary={"tool_name": "bash"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    [row] = _read_audit_rows(tmp_path, record)
    assert row["reason_detail"] == "matched permission rule"
    assert "rule" not in row
    assert row["rule_fingerprint"] == fingerprint_text("bash(echo secret-value)")
    assert "secret-value" not in json.dumps(row)


def test_emit_permission_audit_preserves_safe_multi_rule_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    rule = "bash(mkdir:*), bash(rm:*)"
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="bash",
        tool_use_id="tu-private",
        decision="allow",
        scope="session_rule",
        source="repl_prompt",
        rule_source="session",
        reason_type="prompt_selection",
        reason_detail="always_allow_rule",
        rule=rule,
        operation={"is_read_only": False},
        input_summary={"tool_name": "bash"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    [row] = _read_audit_rows(tmp_path, record)
    assert row["rule"] == rule
    assert row["rule_fingerprint"] == fingerprint_text(rule)


def test_emit_permission_audit_excludes_tool_input_redacted_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        operation={"product": "ros", "action": "CreateStack"},
        input_summary={"tool_name": "aliyun_api"},
        tool_input_redacted={"access_key_secret": "secret-value"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    rows = _read_audit_rows(tmp_path, record)
    assert "tool_input_redacted" not in rows[0]
    assert "secret-value" not in json.dumps(rows[0])


def test_build_redacted_tool_input_truncates_deep_and_wide_objects() -> None:
    nested: object = "leaf"
    for _ in range(100):
        nested = {"next": nested}
    wide = {f"field_{index}": index for index in range(100)}

    redacted = build_redacted_tool_input({"nested": nested, "wide": wide})

    nested_key = fingerprint_text("nested")
    wide_key = fingerprint_text("wide")
    assert _has_truncated_object(redacted[nested_key])
    assert redacted[wide_key]["_truncated"] == {"type": "object", "truncated": True}
    assert "field_99" not in redacted[wide_key]


def test_build_redacted_tool_input_fingerprints_business_field_names() -> None:
    redacted = build_redacted_tool_input(
        {
            "command": "git status",
            "customerEmail": "alice@example.com",
            "customer-prod-123": "tenant-id",
        }
    )

    serialized = json.dumps(redacted)
    assert "command" in redacted
    assert "customerEmail" not in serialized
    assert "customer-prod-123" not in serialized
    assert redacted[fingerprint_text("customerEmail")] == {
        "type": "str",
        "length": len("alice@example.com"),
        "fingerprint": fingerprint_text("alice@example.com"),
    }
    assert redacted[fingerprint_text("customer-prod-123")] == {
        "type": "str",
        "length": len("tenant-id"),
        "fingerprint": fingerprint_text("tenant-id"),
    }


def test_emit_permission_audit_truncates_redacted_tool_input_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    nested: object = "leaf"
    for _ in range(100):
        nested = {"next": nested}
    wide = {f"field_{index}": index for index in range(100)}
    record = PermissionAuditRecord(
        session_id="s-truncate",
        tool_name="bash",
        tool_use_id="tu-truncate",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        tool_input_redacted={"nested": nested, "wide": wide},
    )

    emit_permission_audit(
        record,
        settings=PermissionAuditSettings(include_tool_input=True, max_file_bytes=1024 * 1024, max_files=2),
    )

    [row] = _read_audit_rows(tmp_path, record)
    nested_key = fingerprint_text("nested")
    wide_key = fingerprint_text("wide")
    assert _has_truncated_object(row["tool_input_redacted"][nested_key])
    assert row["tool_input_redacted"][wide_key]["_truncated"] == {"type": "object", "truncated": True}
    assert "field_99" not in row["tool_input_redacted"][wide_key]


def test_emit_permission_audit_includes_redacted_tool_input_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        operation={"product": "ros", "action": "CreateStack"},
        input_summary={"tool_name": "aliyun_api"},
        tool_input_redacted={
            "accessKeyId": "ak-secret",
            "apiKey": "api-secret",
            "x-api-key": "dash-secret",
            "cookie": "session-secret",
            "Signature": "signature-secret",
            "params": {
                "StackName": "demo",
                "AccessKeySecret": "secret-value",
                "private_key": "private-secret",
                "token=secret-token": "value",
            },
            "headers": {"Authorization": "bearer secret-token", "apiKey=api-secret": "value"},
            "note": "token=secret-token",
        },
    )

    emit_permission_audit(
        record,
        settings=PermissionAuditSettings(include_tool_input=True, max_file_bytes=1024, max_files=2),
    )

    rows = _read_audit_rows(tmp_path, record)
    assert rows[0]["tool_input_redacted"] == {
        fingerprint_text("accessKeyId"): {"redacted": True},
        fingerprint_text("apiKey"): {"redacted": True},
        fingerprint_text("x-api-key"): {"redacted": True},
        fingerprint_text("cookie"): {"redacted": True},
        fingerprint_text("Signature"): {"redacted": True},
        "params": {
            fingerprint_text("StackName"): {"type": "str", "length": 4, "fingerprint": fingerprint_text("demo")},
            fingerprint_text("AccessKeySecret"): {"redacted": True},
            fingerprint_text("private_key"): {"redacted": True},
            fingerprint_text("token=secret-token"): {"redacted": True},
        },
        "headers": {
            fingerprint_text("Authorization"): {"redacted": True},
            fingerprint_text("apiKey=api-secret"): {"redacted": True},
        },
        fingerprint_text("note"): {"type": "str", "length": 18, "fingerprint": fingerprint_text("token=secret-token")},
    }
    assert "ak-secret" not in json.dumps(rows[0])
    assert "api-secret" not in json.dumps(rows[0])
    assert "dash-secret" not in json.dumps(rows[0])
    assert "session-secret" not in json.dumps(rows[0])
    assert "signature-secret" not in json.dumps(rows[0])
    assert "private-secret" not in json.dumps(rows[0])
    assert "secret-value" not in json.dumps(rows[0])
    assert "secret-token" not in json.dumps(rows[0])
    assert "accessKeyId" not in json.dumps(rows[0])
    assert "AccessKeySecret" not in json.dumps(rows[0])
    assert "Signature" not in json.dumps(rows[0])
    assert "private_key" not in json.dumps(rows[0])
    assert "apiKey=api-secret" not in json.dumps(rows[0])
    assert "demo" not in json.dumps(rows[0])


def test_emit_permission_audit_redacts_embedded_json_and_pem_strings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        operation={"product": "ros", "action": "CreateStack"},
        input_summary={"tool_name": "aliyun_api"},
        tool_input_redacted={
            "params": {
                "payload": '{"AccessKeySecret":"super-secret"}',
                "TemplateBody": pem,
            },
        },
    )

    emit_permission_audit(
        record,
        settings=PermissionAuditSettings(include_tool_input=True, max_file_bytes=1024, max_files=2),
    )

    [row] = _read_audit_rows(tmp_path, record)
    rendered = json.dumps(row, ensure_ascii=False)
    assert "super-secret" not in rendered
    assert "private-body" not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered
    assert "AccessKeySecret" not in rendered


def test_emit_permission_audit_tool_input_redaction_omits_business_payloads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", Mock())
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        operation={"product": "ros", "action": "CreateStack"},
        input_summary={"tool_name": "aliyun_api"},
        tool_input_redacted={
            "product": "ros",
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
            "params": {
                "StackName": "customer-stack-name",
                "TemplateBody": "Resources: proprietary-template-body",
                "UserData": "#!/bin/bash\ncurl https://internal.example",
                "CommandContent": "echo proprietary-command",
            },
            "body": {"PolicyDocument": '{"Statement": "customer-policy"}'},
        },
    )

    emit_permission_audit(
        record,
        settings=PermissionAuditSettings(include_tool_input=True, max_file_bytes=1024, max_files=2),
    )

    [row] = _read_audit_rows(tmp_path, record)
    serialized = json.dumps(row, ensure_ascii=False)
    assert row["tool_input_redacted"]["product"] == {
        "type": "str",
        "length": 3,
        "fingerprint": fingerprint_text("ros"),
    }
    assert "customer-stack-name" not in serialized
    assert "proprietary-template-body" not in serialized
    assert "internal.example" not in serialized
    assert "proprietary-command" not in serialized
    assert "customer-policy" not in serialized


def test_emit_permission_audit_uses_minimal_private_telemetry_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    telemetry = Mock()
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", telemetry)
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="aliyun_api",
        tool_use_id="tu-private",
        decision="deny",
        scope="settings_rule",
        source="permission_pipeline",
        rule_source="project_settings",
        reason_type="rule",
        reason_detail="token=secret-token permission denied",
        rule="ros:DeleteStack",
        rule_fingerprint="raw-rule-fingerprint-secret",
        operation={
            "product": "ros*operation-secret",
            "action": "DeleteStack operation-secret",
            "region": "cn-hangzhou/operation-secret",
            "product_fingerprint": "raw-product-fingerprint-secret",
            "action_fingerprint": "raw-action-fingerprint-secret",
            "region_fingerprint": "raw-region-fingerprint-secret",
            "is_read_only": False,
            "access_key_secret": "operation-secret",
        },
        input_summary={"tool_name": "aliyun_api", "params_fields": ["StackName"]},
        tool_input_redacted={"params": {"StackName": {"redacted": True}}},
    )

    emit_permission_audit(
        record,
        settings=PermissionAuditSettings(include_tool_input=True, max_file_bytes=1024, max_files=2),
    )

    rows = _read_audit_rows(tmp_path, record)
    assert rows[0]["reason_detail"] == "matched permission rule"
    assert rows[0]["tool_input_redacted"] == {"params": {fingerprint_text("StackName"): {"redacted": True}}}
    assert rows[0]["operation"] == {
        "product_fingerprint": fingerprint_text("ros*operation-secret"),
        "action_fingerprint": fingerprint_text("DeleteStack operation-secret"),
        "region_fingerprint": fingerprint_text("cn-hangzhou/operation-secret"),
        "is_read_only": False,
    }
    audit_serialized = json.dumps(rows[0])
    for forbidden in (
        "access_key_secret",
        "operation-secret",
        "ros*operation-secret",
        "DeleteStack operation-secret",
        "cn-hangzhou/operation-secret",
        "raw-rule-fingerprint-secret",
        "raw-product-fingerprint-secret",
        "raw-action-fingerprint-secret",
        "raw-region-fingerprint-secret",
    ):
        assert forbidden not in audit_serialized

    telemetry.assert_called_once()
    metadata = telemetry.call_args.args[1]
    assert metadata == {
        "tool_name": "aliyun_api",
        "decision": "deny",
        "scope": "settings_rule",
        "source": "permission_pipeline",
        "rule_source": "project_settings",
        "reason_type": "rule",
        "product_fingerprint": fingerprint_text("ros*operation-secret"),
        "action_fingerprint": fingerprint_text("DeleteStack operation-secret"),
        "region_fingerprint": fingerprint_text("cn-hangzhou/operation-secret"),
        "is_read_only": False,
        "rule_fingerprint": fingerprint_text("ros:DeleteStack"),
    }
    serialized = json.dumps(metadata)
    for forbidden in (
        "session_id",
        "tool_use_id",
        "timestamp",
        "input_summary",
        "tool_input_redacted",
        "reason_detail",
        "s-private",
        "tu-private",
        "secret-token",
        "operation-secret",
        "StackName",
    ):
        assert forbidden not in serialized


def test_emit_permission_audit_sanitizes_unsafe_telemetry_tokens(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    telemetry = Mock()
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", telemetry)
    record = PermissionAuditRecord(
        session_id="s-private",
        tool_name="bash",
        tool_use_id="tu-private",
        decision="deny",
        scope="settings_rule",
        source="permission_pipeline",
        rule_source="secret=abc",
        reason_type="token=secret-token",
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    telemetry.assert_called_once()
    metadata = telemetry.call_args.args[1]
    assert metadata["rule_source"] == fingerprint_text("secret=abc")
    assert metadata["reason_type"] == fingerprint_text("token=secret-token")
    serialized = json.dumps(metadata)
    assert "secret=abc" not in serialized
    assert "token=secret-token" not in serialized


def test_emit_permission_audit_telemetry_failure_does_not_prevent_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    telemetry = Mock(side_effect=RuntimeError("telemetry unavailable"))
    monkeypatch.setattr("iac_code.services.permissions.audit.log_event", telemetry)
    record = PermissionAuditRecord(
        session_id="s1",
        tool_name="aliyun_api",
        tool_use_id="tu1",
        decision="allow",
        scope="settings_rule",
        source="permission_pipeline",
        operation={"product": "ros", "action": "CreateStack", "region": "cn-hangzhou"},
        input_summary={"tool_name": "aliyun_api"},
    )

    emit_permission_audit(record, settings=PermissionAuditSettings(max_file_bytes=1024, max_files=2))

    rows = _read_audit_rows(tmp_path, record)
    assert rows[0]["decision"] == "allow"
    telemetry.assert_called_once()
