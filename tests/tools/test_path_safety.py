from iac_code.tools.path_safety import (
    SENSITIVE_PATHS,
    ReadPathDecision,
    _path_hits_sensitive,
    _path_is_under,
    check_read_path,
    get_iac_code_application_root,
    is_sensitive_path,
)
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult


def test_project_file_is_allowed(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(tmp_path), additional_directories=[], trusted_read_directories=[])

    assert result == ReadPathDecision("allow")


def test_additional_directory_file_is_allowed(tmp_path):
    cwd = tmp_path / "project"
    shared = tmp_path / "shared"
    cwd.mkdir()
    shared.mkdir()
    target = shared / "notes.txt"
    target.write_text("notes", encoding="utf-8")

    result = check_read_path(
        str(target),
        cwd=str(cwd),
        additional_directories=[str(shared)],
        trusted_read_directories=[],
    )

    assert result == ReadPathDecision("allow")


def test_iac_code_application_root_is_allowed(tmp_path):
    app_root = get_iac_code_application_root()
    target = app_root / "__init__.py"
    if not target.exists():
        target = next(app_root.rglob("__init__.py"))

    result = check_read_path(
        str(target),
        cwd=str(tmp_path),
        additional_directories=[],
        trusted_read_directories=[],
    )

    assert result.behavior == "allow"


def test_project_outside_file_asks(tmp_path):
    cwd = tmp_path / "project"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(cwd), additional_directories=[], trusted_read_directories=[])

    assert result.behavior == "ask"
    assert result.reason_type == "path_constraint"


def test_sensitive_path_asks_even_inside_cwd(tmp_path):
    target = tmp_path / ".env"
    target.write_text("TOKEN=fake", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(tmp_path), additional_directories=[], trusted_read_directories=[])

    assert result.behavior == "ask"
    assert result.reason_type == "safety_check"


def test_iac_code_credentials_are_sensitive(tmp_path):
    target = tmp_path / ".iac-code" / ".credentials.yml"
    target.parent.mkdir()
    target.write_text("openai: fake", encoding="utf-8")

    assert is_sensitive_path(str(target)) is True


def test_trusted_read_directory_is_allowed(tmp_path):
    cwd = tmp_path / "project"
    trusted = tmp_path / ".iac-code" / "tool-results" / "session-1"
    cwd.mkdir()
    trusted.mkdir(parents=True)
    target = trusted / "tool.txt"
    target.write_text("large result", encoding="utf-8")

    result = check_read_path(
        str(target),
        cwd=str(cwd),
        additional_directories=[],
        trusted_read_directories=[str(trusted)],
    )

    assert result.behavior == "allow"


def test_read_path_allow_decision_converts_to_passthrough_permission_result():
    result = ReadPathDecision("allow").to_permission_result()

    assert result == PermissionResult(behavior="passthrough")


def test_read_path_ask_decision_converts_to_ask_permission_result():
    decision = ReadPathDecision("ask", reason_type="path_constraint", detail="path outside allowed directories")

    result = decision.to_permission_result()

    assert result == PermissionResult(
        behavior="ask",
        message="path outside allowed directories",
        reason=PermissionDecisionReason(type="path_constraint", detail="path outside allowed directories"),
    )


def test_iac_code_credential_files_are_explicit_sensitive_entries():
    assert ".iac-code/.credentials.yml" in SENSITIVE_PATHS
    assert ".iac-code/.cloud-credentials.yml" in SENSITIVE_PATHS


def test_windows_sensitive_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")

    assert _path_hits_sensitive("C:\\Users\\me\\NTUSER.DAT")
    assert _path_hits_sensitive("C:\\Users\\me\\appdata\\local\\microsoft\\credentials\\data")


def test_macos_sensitive_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")

    assert _path_hits_sensitive("/Users/me/project/.SSH/id_rsa")
    assert _path_hits_sensitive("/Users/me/project/.IAC-CODE/.CREDENTIALS.yml")


def test_windows_root_containment_is_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    root = tmp_path / "Project"
    child = root / "src" / "app.py"

    assert _path_is_under(str(child).upper(), str(root).lower())
