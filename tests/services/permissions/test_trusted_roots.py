from pathlib import Path

import pytest


def test_build_session_trusted_read_directories_uses_session_artifact_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("iac_code.services.permissions.trusted_roots.get_config_dir", lambda: tmp_path / ".iac-code")

    from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

    roots = build_session_trusted_read_directories("abc123")

    assert roots == [
        str(Path(tmp_path / ".iac-code" / "tool-results" / "abc123")),
        str(Path(tmp_path / ".iac-code" / "image-cache" / "abc123")),
    ]


def test_build_session_trusted_read_directories_returns_empty_for_falsey_session_id():
    from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

    assert build_session_trusted_read_directories(None) == []
    assert build_session_trusted_read_directories("") == []


@pytest.mark.parametrize("session_id", [".", "..", "../memory", "a/b", "a\\b"])
def test_build_session_trusted_read_directories_rejects_path_shaped_session_ids(session_id):
    from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

    with pytest.raises(ValueError):
        build_session_trusted_read_directories(session_id)
