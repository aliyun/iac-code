from __future__ import annotations

import ntpath

from iac_code.utils.public_paths import build_public_path_roots, redact_known_public_paths, sanitize_public_paths


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


def test_redact_known_public_paths_replaces_only_confirmed_server_paths() -> None:
    roots = [{"path": "/server-root", "label": "."}]

    assert (
        redact_known_public_paths(
            "server=/server-root/private/file target=/home/cloud-user/bootstrap.sh",
            roots,
        )
        == "server=[PATH] target=/home/cloud-user/bootstrap.sh"
    )


def test_redact_known_public_paths_preserves_narrative_text_after_path() -> None:
    roots = [{"path": "/srv/iac-code", "label": "."}]

    cases = {
        "failed at /srv/iac-code/private/result.json while reading": "failed at [PATH] while reading",
        "failed at /srv/iac-code/private/result.json and retry": "failed at [PATH] and retry",
        "failed at /srv/iac-code/private/result.json please retry": "failed at [PATH] please retry",
        "path /srv/iac-code/private/result.json was missing": "path [PATH] was missing",
    }

    for value, expected in cases.items():
        assert redact_known_public_paths(value, roots) == expected


def test_redact_known_public_paths_supports_full_and_quoted_paths_with_spaces() -> None:
    roots = [{"path": "/srv/iac-code", "label": "."}]

    assert redact_known_public_paths("/srv/iac-code/Plan A/result.json", roots) == "[PATH]"
    assert (
        redact_known_public_paths('failed at "/srv/iac-code/Plan A/result.json" please retry', roots)
        == 'failed at "[PATH]" please retry'
    )


def test_redact_known_public_paths_preserves_text_after_windows_path() -> None:
    roots = [{"path": r"C:\iac-code", "label": "."}]

    assert (
        redact_known_public_paths(r"failed at C:\iac-code\private\result.json please retry", roots)
        == "failed at [PATH] please retry"
    )


def test_redact_known_public_paths_replaces_confirmed_file_uri_only() -> None:
    roots = [{"path": "/server-root", "label": "."}]

    assert (
        redact_known_public_paths(
            "local=file:///server-root/private/result.json remote=file:///home/cloud-user/result.json",
            roots,
        )
        == "local=[PATH] remote=file:///home/cloud-user/result.json"
    )


def test_redact_known_public_paths_uses_exact_placeholder_for_windows_and_unc() -> None:
    roots = [
        {"path": r"C:\\iac-code\\workspace", "label": "."},
        {"path": r"\\\\server\\share", "label": "[trusted]"},
    ]

    assert redact_known_public_paths(r"C:\\iac-code\\workspace\\a.txt", roots) == "[PATH]"
    assert redact_known_public_paths(r"\\\\server\\share\\a.txt", roots) == "[PATH]"
