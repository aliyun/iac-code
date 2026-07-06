from __future__ import annotations

import ntpath

from iac_code.utils.public_paths import build_public_path_roots, sanitize_public_paths


def test_build_public_path_roots_includes_config_and_trusted_directories(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    roots = build_public_path_roots(
        cwd=str(tmp_path / "workspace"),
        additional_directories=[str(tmp_path / "additional")],
        trusted_read_directories=[str(config_dir / "tool-results" / "session-1")],
        relative_read_directories=[str(tmp_path / "skills")],
    )

    assert roots == [
        {"path": str(tmp_path / "workspace"), "label": "."},
        {"path": str(config_dir), "label": "$IAC_CODE_CONFIG_DIR"},
        {"path": str(tmp_path / "additional"), "label": "[trusted]"},
        {"path": str(config_dir / "tool-results" / "session-1"), "label": "[trusted]"},
        {"path": str(tmp_path / "skills"), "label": "[trusted]"},
    ]


def test_sanitize_public_paths_handles_space_separated_absolute_paths(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    roots = build_public_path_roots(cwd=str(cwd))

    sanitized = sanitize_public_paths(
        "paths: {} {} /opt/iac-code-outside/config.yaml".format(
            cwd / "src" / "app.py",
            config_dir / "tool-results" / "session-1" / "result.txt",
        ),
        roots,
    )

    assert sanitized == "paths: ./src/app.py $IAC_CODE_CONFIG_DIR/tool-results/session-1/result.txt [PATH]"


def test_sanitize_public_paths_handles_space_separated_windows_paths() -> None:
    roots = [
        {"path": r"C:\Users\alice\project", "label": "."},
        {"path": r"C:\Users\alice\config", "label": "$IAC_CODE_CONFIG_DIR"},
    ]

    value = (
        r"paths: C:\Users\alice\project\src\app.py "
        r"C:\Users\alice\config\tool-results\session-1\result.txt "
        r"C:\outside\config.yaml"
    )
    sanitized = sanitize_public_paths(
        value,
        roots,
    )

    assert sanitized == "paths: ./src/app.py $IAC_CODE_CONFIG_DIR/tool-results/session-1/result.txt [PATH]"


def test_sanitize_public_paths_keeps_posix_roots_platform_independent(monkeypatch) -> None:
    monkeypatch.setattr("iac_code.utils.public_paths.os.path.abspath", ntpath.abspath)
    monkeypatch.setattr("iac_code.utils.public_paths.os.path.realpath", ntpath.abspath)

    sanitized = sanitize_public_paths(
        "paths: /Users/alice/project/src/app.py /Users/alice/project/logs/result.txt",
        [{"path": "/Users/alice/project", "label": "."}],
    )

    assert sanitized == "paths: ./src/app.py ./logs/result.txt"
