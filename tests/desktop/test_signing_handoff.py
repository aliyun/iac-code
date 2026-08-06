from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _load_script():
    scripts = ROOT / "desktop/scripts"
    sys.path.insert(0, str(scripts))
    script = scripts / "signing_handoff.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_signing_handoff", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_tree(root: Path, platform_name: str) -> None:
    suffix = ".exe" if platform_name == "windows-x64" else ""
    channel = "windows" if platform_name == "windows-x64" else "macos"
    helper = "iac-code-desktop-updater.exe" if platform_name == "windows-x64" else "iac-code-desktop-exec"
    files = {
        root / "desktop/src-tauri/target/release/{}{}".format("iac-code-desktop", suffix): b"host",
        root / "desktop/dist/native-helpers" / channel / "release" / helper: b"helper",
        root / "desktop/dist/sidecar/iac-code-sidecar" / "iac-code-sidecar{}".format(suffix): b"sidecar",
        root / "desktop/dist/sidecar/iac-code-sidecar" / "library.dat": b"library",
    }
    if platform_name == "windows-x64":
        files[
            root / "desktop/dist/native-helpers/windows/release/iac-code-desktop-updater.manifest.json"
        ] = json.dumps({"authenticodeRequired": False}).encode()
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if platform_name == "macos-aarch64":
        dylib = root / "desktop/dist/sidecar/iac-code-sidecar/_internal/PIL/.dylibs/libexample.dylib"
        dylib.parent.mkdir(parents=True, exist_ok=True)
        dylib.write_bytes(b"dylib")
        link = root / "desktop/dist/sidecar/iac-code-sidecar/_internal/libexample.dylib"
        link.symlink_to("PIL/.dylibs/libexample.dylib")


@pytest.mark.parametrize("platform_name", ["macos-aarch64", "windows-x64"])
def test_signing_handoff_is_deterministic_and_verifies_identity(tmp_path: Path, platform_name: str) -> None:
    module = _load_script()
    source = tmp_path / "source"
    _fixture_tree(source, platform_name)
    manifest = module.build_manifest(
        source,
        repository="aliyun/iac-code",
        tag="v0.11.1",
        version="0.11.1",
        commit="b" * 40,
        platform_name=platform_name,
        stage="unsigned-components",
    )
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    module.create_archive(source, first, manifest)
    module.create_archive(source, second, manifest)

    assert first.read_bytes() == second.read_bytes()
    verified = module.verify_archive(
        first,
        repository="aliyun/iac-code",
        tag="v0.11.1",
        version="0.11.1",
        commit="b" * 40,
        platform_name=platform_name,
        required_stage="unsigned-components",
    )
    assert verified["signTargets"]
    if platform_name == "macos-aarch64":
        symlinks = [item for item in verified["files"] if item["type"] == "symlink"]
        assert len(symlinks) == 1
        extracted = tmp_path / "extracted"
        module.extract_archive(first, extracted, verified)
        link = extracted / symlinks[0]["path"]
        assert link.is_symlink()
        assert link.readlink().as_posix() == "PIL/.dylibs/libexample.dylib"

    with pytest.raises(RuntimeError, match="identity mismatch"):
        module.verify_archive(
            first,
            repository="aliyun/iac-code",
            tag="v0.11.1",
            version="0.11.1",
            commit="c" * 40,
            platform_name=platform_name,
            required_stage="unsigned-components",
        )


def test_signed_handoff_requires_publisher_evidence(tmp_path: Path) -> None:
    module = _load_script()
    _fixture_tree(tmp_path, "windows-x64")

    with pytest.raises(RuntimeError, match="publisher evidence"):
        module.build_manifest(
            tmp_path,
            repository="aliyun/iac-code",
            tag="v0.11.1",
            version="0.11.1",
            commit="b" * 40,
            platform_name="windows-x64",
            stage="signed-components",
        )
