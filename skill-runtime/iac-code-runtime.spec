import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(os.environ["IAC_CODE_SKILL_RUNTIME_ROOT"]).resolve()
STAGING = Path(os.environ["IAC_CODE_SKILL_RUNTIME_STAGING"]).resolve()
PACKAGE = STAGING / "iac_code"

hiddenimports = sorted(
    set(
        collect_submodules("keyring.backends")
        + collect_submodules("a2a")
        + collect_submodules("tiktoken_ext")
        + collect_submodules("opentelemetry")
        + collect_submodules("alibabacloud_oss_v2")
        + collect_submodules("iac_code.providers")
        + collect_submodules("iac_code.tools.cloud.aliyun.hooks")
        + collect_submodules("uvicorn.protocols")
        + collect_submodules("uvicorn.lifespan")
        + collect_submodules("uvicorn.loops")
    )
)

datas = []
for source in PACKAGE.rglob("*"):
    if source.is_file() and source.suffix not in {".py", ".pyc"}:
        datas.append((str(source), str(source.parent.relative_to(STAGING))))

for pattern in (
    "tools/cloud/aliyun/hooks/*.py",
    "skills/bundled/*/auto_trigger.py",
    "skills/bundled/iac_aliyun/scripts/tf2ros.py",
    "pipeline/*/tools/*.py",
    "pipeline/*/hooks/*.py",
):
    for source in PACKAGE.glob(pattern):
        datas.append((str(source), str(source.parent.relative_to(STAGING))))

runtime = Analysis(
    [str(ROOT / "skill-runtime/entry.py")],
    pathex=[str(STAGING)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
runtime_pyz = PYZ(runtime.pure)
runtime_exe = EXE(
    runtime_pyz,
    runtime.scripts,
    [],
    exclude_binaries=True,
    name="iac-code",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    runtime_exe,
    runtime.binaries,
    runtime.datas,
    strip=False,
    upx=False,
    name="iac-code-runtime",
)
