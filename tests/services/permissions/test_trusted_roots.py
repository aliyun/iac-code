import importlib
from pathlib import Path

import pytest


def test_build_session_trusted_read_directories_uses_session_artifact_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("iac_code.config.get_config_dir", lambda: tmp_path / ".iac-code")

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


def test_build_session_trusted_read_directories_does_not_retain_patched_config_dir(monkeypatch, tmp_path):
    import iac_code.config as config
    import iac_code.services.permissions.trusted_roots as trusted_roots

    fake_config_dir = Path("/tmp/iac-config")
    with monkeypatch.context() as patch:
        patch.setattr(config, "get_config_dir", lambda: fake_config_dir)
        trusted_roots = importlib.reload(trusted_roots)

        assert trusted_roots.build_session_trusted_read_directories("old") == [
            str(fake_config_dir / "tool-results" / "old"),
            str(fake_config_dir / "image-cache" / "old"),
        ]

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    assert trusted_roots.build_session_trusted_read_directories("new") == [
        str(tmp_path / "config" / "tool-results" / "new"),
        str(tmp_path / "config" / "image-cache" / "new"),
    ]
