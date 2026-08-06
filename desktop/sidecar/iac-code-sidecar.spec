import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(os.environ["IAC_CODE_DESKTOP_ROOT"]).resolve()
STAGING = Path(os.environ["IAC_CODE_DESKTOP_STAGING"]).resolve()
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

# Import the actual native consumers/backends before SetDllDirectoryW(NULL).
# Package roots are insufficient because provider SDKs and keyring/tiktoken
# keep their native imports lazy until these concrete modules are loaded.
native_preload_sources = {
    "keyring.backends.Windows": "keyring.backends",
    "tiktoken_ext.openai_public": "tiktoken_ext",
    "opentelemetry.sdk.trace.export": "opentelemetry",
    "iac_code.services.telemetry.client": "opentelemetry",
    "iac_code.tools.cloud.aliyun.oss_v4_adapter": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.bucket_basic": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.bucket_object_worm_configuration": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.data_process": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.object_basic": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.object_worm": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.region": "alibabacloud_oss_v2",
    "alibabacloud_oss_v2.models.service": "alibabacloud_oss_v2",
    "iac_code.providers.anthropic_provider": "iac_code.providers",
    "iac_code.providers.azure_openai_provider": "iac_code.providers",
    "iac_code.providers.dashscope_provider": "iac_code.providers",
    "iac_code.providers.deepseek_provider": "iac_code.providers",
    "iac_code.providers.gemini_provider": "iac_code.providers",
    "iac_code.providers.kimi_provider": "iac_code.providers",
    "iac_code.providers.lmstudio_provider": "iac_code.providers",
    "iac_code.providers.minimax_provider": "iac_code.providers",
    "iac_code.providers.modelscope_provider": "iac_code.providers",
    "iac_code.providers.ollama_provider": "iac_code.providers",
    "iac_code.providers.openai_provider": "iac_code.providers",
    "iac_code.providers.openrouter_provider": "iac_code.providers",
    "iac_code.providers.siliconflow_provider": "iac_code.providers",
    "iac_code.providers.volcengine_provider": "iac_code.providers",
    "iac_code.providers.zhipu_provider": "iac_code.providers",
}
for module, discovery_root in native_preload_sources.items():
    if discovery_root not in hiddenimports and not any(name.startswith(discovery_root + ".") for name in hiddenimports):
        raise RuntimeError("native preload module is absent from hidden-import discovery: {}".format(module))
(PACKAGE / "desktop/native_preload_manifest.json").write_text(
    json.dumps({"modules": tuple(native_preload_sources)}, indent=2) + "\n",
    encoding="utf-8",
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

sidecar = Analysis(
    [str(ROOT / "desktop/sidecar/sidecar_entry.py")],
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
sidecar_pyz = PYZ(sidecar.pure)
sidecar_exe = EXE(
    sidecar_pyz,
    sidecar.scripts,
    [],
    exclude_binaries=True,
    name="iac-code-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

tf2ros = Analysis(
    [str(ROOT / "desktop/sidecar/tf2ros_entry.py")],
    pathex=[str(STAGING)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
tf2ros_pyz = PYZ(tf2ros.pure)
tf2ros_exe = EXE(
    tf2ros_pyz,
    tf2ros.scripts,
    [],
    exclude_binaries=True,
    name="iac-code-tf2ros",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    sidecar_exe,
    tf2ros_exe,
    sidecar.binaries,
    sidecar.datas,
    tf2ros.binaries,
    tf2ros.datas,
    strip=False,
    upx=False,
    name="iac-code-sidecar",
)
