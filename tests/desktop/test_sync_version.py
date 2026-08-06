from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_script():
    script = Path(__file__).parents[2] / "desktop/scripts/sync_version.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_sync_version", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_version_fixture(root: Path) -> None:
    files = {
        "src/iac_code/__init__.py": '__version__ = "1.2.3"\n',
        "desktop/package.json": '{"name":"iac-code-desktop","version":"0.1.0"}\n',
        "desktop/package-lock.json": (
            '{"name":"iac-code-desktop","version":"0.1.0","packages":'
            '{"":{"name":"iac-code-desktop","version":"0.1.0"}}}\n'
        ),
        "desktop/src-tauri/tauri.conf.json": '{\n  "version": "0.1.0"\n}\n',
        "desktop/src-tauri/Cargo.toml": '[package]\nname = "iac-code-desktop"\nversion = "0.1.0"\n',
        "desktop/helpers/Cargo.toml": '[package]\nname = "iac-code-desktop-helpers"\nversion = "0.1.0"\n',
        "desktop/src-tauri/Cargo.lock": (
            '[[package]]\nname = "iac-code-desktop"\nversion = "0.1.0"\n\n'
            '[[package]]\nname = "iac-code-desktop-helpers"\nversion = "0.1.0"\n'
        ),
        "desktop/helpers/Cargo.lock": (
            '[[package]]\nname = "iac-code-desktop-helpers"\nversion = "0.1.0"\n'
        ),
    }
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_sync_version_uses_python_package_as_the_only_source(tmp_path: Path) -> None:
    module = _load_script()
    _write_version_fixture(tmp_path)

    assert module.sync_desktop_versions(tmp_path, check=True)
    changed = module.sync_desktop_versions(tmp_path)
    assert len(changed) == 7
    assert module.sync_desktop_versions(tmp_path, check=True) == []

    assert json.loads((tmp_path / "desktop/package.json").read_text(encoding="utf-8"))["version"] == "1.2.3"
    assert json.loads((tmp_path / "desktop/package-lock.json").read_text(encoding="utf-8"))["packages"][""][
        "version"
    ] == "1.2.3"
    for relative in (
        "desktop/src-tauri/Cargo.toml",
        "desktop/helpers/Cargo.toml",
        "desktop/src-tauri/Cargo.lock",
        "desktop/helpers/Cargo.lock",
    ):
        assert 'version = "1.2.3"' in (tmp_path / relative).read_text(encoding="utf-8")
