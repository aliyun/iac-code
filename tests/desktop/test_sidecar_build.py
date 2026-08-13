from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _build_module():
    script = Path(__file__).parents[2] / "desktop/scripts/build_sidecar.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_build_sidecar", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sidecar_staging_materializes_resources_and_desktop_helper_instructions(tmp_path: Path) -> None:
    module = _build_module()
    package = module.prepare_staging(tmp_path / "staging", warm_tokenizers=False)

    assert not (package / "pipeline/selling/references").exists()
    for skill in ("iac-aliyun-template-generating", "iac-aliyun-cost", "iac-aliyun-deploying"):
        references = package / "pipeline/selling/skills" / skill / "references"
        assert references.is_dir() and not references.is_symlink()
        assert {path.name for path in (references / "cloud-products").glob("*.md")} >= {
            "ecs.md",
            "rds.md",
            "redis.md",
            "slb.md",
            "vpc.md",
            "oss.md",
            "ga.md",
        }
    assert list((package / "i18n/locales").glob("*/LC_MESSAGES/messages.mo"))
    assert list((package / "i18n/locales").glob("*/LC_MESSAGES/webui.mo"))
    assert (package / "pipeline/engine/architecture_rules.json").is_file()
    assert (package / "tools/cloud/aliyun/ros_validation/data/ros_official_resource_index.json").is_file()
    markdown = list(package.rglob("*.md"))
    assert markdown
    assert all("tf2ros.py" not in path.read_text(encoding="utf-8") for path in markdown)
    assert any("iac-code-tf2ros" in path.read_text(encoding="utf-8") for path in markdown)
    assert not any(path.is_symlink() for path in package.rglob("*"))


def test_sidecar_release_build_stamps_stable_published_date_only_in_staging(tmp_path: Path) -> None:
    module = _build_module()
    source_init = module.SOURCE_PACKAGE / "__init__.py"
    source_before = source_init.read_text(encoding="utf-8")

    package = module.prepare_staging(
        tmp_path / "staging",
        warm_tokenizers=False,
        release_date="2026-08-04T12:00:00Z",
    )

    assert '__release_date__ = "2026-08-04"' in (package / "__init__.py").read_text(encoding="utf-8")
    assert source_init.read_text(encoding="utf-8") == source_before


@pytest.mark.parametrize("value", ("", "not-a-date", "2026-13-40"))
def test_sidecar_release_date_rejects_invalid_values(value: str) -> None:
    module = _build_module()

    with pytest.raises(ValueError, match="release date"):
        module.normalize_release_date(value)


def test_release_sidecar_build_requires_release_date(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "desktop/scripts/build_sidecar.py"
    environment = {**os.environ, "IAC_CODE_DESKTOP_RELEASE": "1"}
    environment.pop("IAC_CODE_DESKTOP_RELEASE_DATE", None)

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--prepare-only",
            "--skip-tokenizers",
            "--staging",
            str(tmp_path / "staging"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode != 0
    assert "release sidecar builds require IAC_CODE_DESKTOP_RELEASE_DATE" in result.stderr


def _fail_resolve_for(module, targets: set[Path]):
    """Return a ``Path.resolve`` replacement that raises ``PermissionError`` for
    the given paths, mimicking Windows opening a git symlink reparse point
    (``WinError 5``) while leaving every other resolution intact."""
    real_resolve = module.Path.resolve

    def guarded(self, *args, **kwargs):
        if self in targets:
            raise PermissionError("WinError 5: Access is denied")
        return real_resolve(self, *args, **kwargs)

    return guarded


def test_placeholder_target_survives_resolve_permission_error(tmp_path: Path, monkeypatch) -> None:
    """A git symlink placeholder must still resolve to its target when
    ``Path.resolve`` on the placeholder itself raises ``PermissionError`` — as
    it does on Windows for symlink reparse points."""
    module = _build_module()
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    (target_dir / "a.md").write_text("hi", encoding="utf-8")
    placeholder = tmp_path / "link"
    placeholder.write_text("real", encoding="utf-8")  # git stores the relative target as file content

    monkeypatch.setattr(module.Path, "resolve", _fail_resolve_for(module, {placeholder}))

    assert module._placeholder_target(placeholder) == target_dir


def test_copy_materialized_handles_placeholder_resolve_permission_error(tmp_path: Path, monkeypatch) -> None:
    """Staging must materialize a placeholder into a real directory even when
    resolving the placeholder path raises ``PermissionError`` on Windows."""
    module = _build_module()
    source = tmp_path / "src"
    real = source / "real"
    real.mkdir(parents=True)
    (real / "a.md").write_text("hi", encoding="utf-8")
    placeholder = source / "link"
    placeholder.write_text("real", encoding="utf-8")

    monkeypatch.setattr(module.Path, "resolve", _fail_resolve_for(module, {placeholder}))

    out = tmp_path / "out"
    module._copy_materialized(source, out)

    assert (out / "real" / "a.md").read_text(encoding="utf-8") == "hi"
    # The placeholder is materialized as a real directory, not copied verbatim.
    assert (out / "link").is_dir() and not (out / "link").is_symlink()
    assert (out / "link" / "a.md").read_text(encoding="utf-8") == "hi"


def test_copy_materialized_reads_symlink_when_resolve_is_denied(tmp_path: Path, monkeypatch) -> None:
    module = _build_module()
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("hi", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip("symlinks are unavailable: {}".format(error))

    monkeypatch.setattr(module.Path, "resolve", _fail_resolve_for(module, {link}))

    out = tmp_path / "out"
    module._copy_materialized(link, out)

    assert (out / "a.md").read_text(encoding="utf-8") == "hi"
    assert out.is_dir() and not out.is_symlink()


def test_frozen_metadata_removes_editable_checkout_provenance(tmp_path: Path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    metadata = bundle / "iac_code.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "direct_url.json").write_text(
        '{{"url":"file://{}"}}'.format(module.ROOT),
        encoding="utf-8",
    )
    (metadata / "METADATA").write_text("Name: iac-code\n", encoding="utf-8")

    module.sanitize_frozen_metadata(bundle)

    assert not (metadata / "direct_url.json").exists()


@pytest.mark.parametrize("relative_path", ("infraguard.cmd", "_internal/infraguard.exe"))
def test_frozen_bundle_rejects_infraguard_shims(tmp_path: Path, relative_path: str) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    shim = bundle / relative_path
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"test shim")

    with pytest.raises(RuntimeError, match="InfraGuard shims"):
        module.validate_frozen_bundle(bundle)


def test_native_preload_manifest_names_concrete_dll_consumers() -> None:
    root = Path(__file__).parents[2]
    payload = json.loads((root / "src/iac_code/desktop/native_preload_manifest.json").read_text(encoding="utf-8"))
    modules = set(payload["modules"])

    assert {
        "keyring.backends.Windows",
        "tiktoken_ext.openai_public",
        "opentelemetry.sdk.trace.export",
        "iac_code.services.telemetry.client",
        "iac_code.tools.cloud.aliyun.oss_v4_adapter",
        "alibabacloud_oss_v2.models.bucket_basic",
        "alibabacloud_oss_v2.models.object_basic",
        "iac_code.providers.openai_provider",
    } <= modules
    assert "alibabacloud_oss_v2" not in modules
    spec = (root / "desktop/sidecar/iac-code-sidecar.spec").read_text(encoding="utf-8")
    assert 'collect_submodules("alibabacloud_oss_v2")' in spec
    assert '"iac_code.services.telemetry.client": "opentelemetry"' in spec
