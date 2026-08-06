#!/usr/bin/env python3
"""Build the offline PyInstaller onedir used by the Tauri Desktop host."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACKAGE = ROOT / "src/iac_code"
SPEC = ROOT / "desktop/sidecar/iac-code-sidecar.spec"
SELLING_REFERENCE_SKILLS = (
    "iac-aliyun-template-generating",
    "iac-aliyun-cost",
    "iac-aliyun-deploying",
)
FORBIDDEN_FROZEN_TOOL_SHIMS = {
    "infraguard",
    "infraguard.exe",
    "infraguard.cmd",
    "infraguard.bat",
}


def _placeholder_target(path: Path) -> Path | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > 4096:
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not raw or "\n" in raw or "\r" in raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    try:
        origin = path.resolve()
    except OSError:
        # ``Path.resolve`` opens the file on Windows and raises PermissionError
        # (WinError 5) on git symlink placeholder reparse points. The
        # self-reference guard is best-effort — skip it rather than discarding
        # the target we already resolved; ``_copy_materialized`` still detects
        # real cycles via its ancestor set.
        return resolved
    return resolved if resolved != origin else None


def _resolve_identity(path: Path) -> Path:
    """Best-effort real-path identity for cycle detection. Falls back to the
    unresolved path when Windows refuses to open a symlink reparse point."""
    try:
        return path.resolve(strict=True)
    except OSError:
        return path


def _read_symlink_target(path: Path) -> Path | None:
    """Resolve a link without opening its Windows reparse point."""
    try:
        target = Path(os.readlink(path))
        candidate = target if target.is_absolute() else path.parent / target
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _copy_materialized(source: Path, target: Path, ancestors: tuple[Path, ...] = ()) -> None:
    if source.is_symlink():
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            resolved = _read_symlink_target(source) or _placeholder_target(source) or source
    else:
        resolved = _placeholder_target(source) or source
    resolved_identity = _resolve_identity(resolved)
    if resolved_identity in ancestors:
        raise RuntimeError("cyclic Desktop staging reference: {}".format(source))
    if resolved.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(resolved.iterdir(), key=lambda item: item.name):
            _copy_materialized(child, target / child.name, (*ancestors, resolved_identity))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved, target, follow_symlinks=True)


def _compile_translations(package: Path) -> None:
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    po_files = sorted((package / "i18n/locales").glob("*/LC_MESSAGES/*.po"))
    if not po_files:
        raise RuntimeError("Desktop staging has no translation catalogs")
    for po_file in po_files:
        with po_file.open("rb") as source:
            catalog = read_po(source)
        with po_file.with_suffix(".mo").open("wb") as output:
            write_mo(output, catalog)


def _rewrite_tf2ros_instructions(package: Path) -> None:
    for markdown in sorted(package.rglob("*.md")):
        content = markdown.read_text(encoding="utf-8")
        rewritten = content.replace("python ../scripts/tf2ros.py", "iac-code-tf2ros")
        rewritten = rewritten.replace("tf2ros.py", "iac-code-tf2ros")
        if rewritten != content:
            markdown.write_text(rewritten, encoding="utf-8")
    leftovers = [path for path in package.rglob("*.md") if "tf2ros.py" in path.read_text(encoding="utf-8")]
    if leftovers:
        raise RuntimeError("Desktop staging still contains tf2ros.py instructions")


def _tree_manifest(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _verify_and_remove_canonical_selling_references(package: Path) -> None:
    canonical = package / "pipeline/selling/references"
    expected = _tree_manifest(canonical)
    cloud_products = {"ecs.md", "rds.md", "redis.md", "slb.md", "vpc.md", "oss.md"}
    if not cloud_products.issubset({path.name for path in (canonical / "cloud-products").glob("*.md")}):
        raise RuntimeError("Desktop selling references are missing cloud-product files")
    for skill in SELLING_REFERENCE_SKILLS:
        reference_root = package / "pipeline/selling/skills" / skill / "references"
        if _tree_manifest(reference_root) != expected:
            raise RuntimeError("Desktop selling reference copy differs for {}".format(skill))
    shutil.rmtree(canonical)


def _warm_tokenizer_cache(package: Path) -> None:
    cache = package / "tokenizer_cache"
    cache.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("TIKTOKEN_CACHE_DIR")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    try:
        import tiktoken

        for name in ("cl100k_base", "o200k_base"):
            if not tiktoken.get_encoding(name).encode("iac-code Desktop"):
                raise RuntimeError("tokenizer cache warmup failed for {}".format(name))
    finally:
        if previous is None:
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
        else:
            os.environ["TIKTOKEN_CACHE_DIR"] = previous


def prepare_staging(staging_root: Path, *, warm_tokenizers: bool = True) -> Path:
    package = staging_root / "iac_code"
    _copy_materialized(SOURCE_PACKAGE, package)
    _compile_translations(package)
    _rewrite_tf2ros_instructions(package)
    _verify_and_remove_canonical_selling_references(package)
    if warm_tokenizers:
        _warm_tokenizer_cache(package)
    return package


def sanitize_frozen_metadata(bundle: Path) -> None:
    """Remove editable-install provenance that exposes the build checkout path."""
    for direct_url in bundle.rglob("direct_url.json"):
        direct_url.unlink()
    checkout = str(ROOT).encode("utf-8")
    leaked = []
    for metadata in bundle.rglob("*.dist-info/*"):
        if metadata.is_file() and checkout in metadata.read_bytes():
            leaked.append(metadata)
    if leaked:
        raise RuntimeError("Desktop frozen metadata contains build paths: {}".format(leaked))


def validate_frozen_bundle(bundle: Path) -> None:
    """Reject developer/test tool shims accidentally collected into a release."""
    forbidden = [
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.name.lower() in FORBIDDEN_FROZEN_TOOL_SHIMS
    ]
    if forbidden:
        raise RuntimeError("Desktop frozen bundle contains InfraGuard shims: {}".format(forbidden))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "desktop/dist/sidecar")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skip-tokenizers", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    temporary = None
    if args.staging is None:
        temporary = tempfile.TemporaryDirectory(prefix="iac-code-desktop-sidecar-")
        staging = Path(temporary.name)
    else:
        staging = args.staging.resolve()
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
    prepare_staging(staging, warm_tokenizers=not args.skip_tokenizers)
    if args.prepare_only:
        print(staging)
        return 0
    # PyInstaller's clean option clears its cache but must not be relied on to
    # remove unrelated files from a previous onedir output. A stale command
    # shim in that directory would otherwise be bundled by Tauri unchanged.
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    environment = {
        **os.environ,
        "IAC_CODE_DESKTOP_ROOT": str(ROOT),
        "IAC_CODE_DESKTOP_STAGING": str(staging),
    }
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output),
        "--workpath",
        str(output / ".work"),
        str(SPEC),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    bundle = output / "iac-code-sidecar"
    sanitize_frozen_metadata(bundle)
    validate_frozen_bundle(bundle)
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
