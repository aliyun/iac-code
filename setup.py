"""Custom build hook to inject __release_date__ at build time."""

import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from setuptools import find_namespace_packages, setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

INIT_PATH = Path("src/iac_code/__init__.py")
LOCALES_DIR = Path("src/iac_code/i18n/locales")
PROJECT_ROOT = Path(__file__).resolve().parent
IAC_ALIYUN_REFERENCES_DIR = PROJECT_ROOT / "src/iac_code/skills/bundled/iac_aliyun/references"
SELLING_IAC_ALIYUN_SKILLS = (
    "iac-aliyun-template-generating",
    "iac-aliyun-cost",
    "iac-aliyun-deploying",
)
INSTALL_REQUIRES = [
    "anthropic>=0.40",
    "pydantic>=2.0",
    "typer>=0.9.0",
    "rich>=13.0",
    "pyyaml>=6.0",
    "pyperclip>=1.8.0",
    "openai>=1.50",
    "httpx>=0.27.0",
    "packaging>=24.0",
    "tiktoken>=0.7.0",
    "jsonschema>=4.20",
    "alibabacloud-ros20190910>=3.0.0",
    "alibabacloud-credentials>=0.3.0",
    "loguru>=0.7.0",
    "opentelemetry-distro>=0.48b0",
    "opentelemetry-exporter-otlp>=1.27.0",
    "agent-client-protocol>=0.9.0",
    "pillow==12.2.0",
    "cryptography>=42.0",
    "keyring>=25.0",
    "tree-sitter>=0.25,<0.26",
    "tree-sitter-bash>=0.25,<0.26",
]
EXTRAS_REQUIRE = {
    "http": [
        "starlette>=0.39.0",
        "uvicorn[standard]>=0.30.0",
    ],
    "a2a": [
        "a2a-sdk[http-server,signing]>=1.0.2,<2",
        "cryptography>=42.0",
        "starlette>=0.39.0",
        "uvicorn[standard]>=0.30.0",
    ],
    "a2a-signing": [
        "a2a-sdk[signing]>=1.0.2,<2",
    ],
    "a2a-grpc": [
        "grpcio>=1.60.0",
        "grpcio-status>=1.60.0",
    ],
    "a2a-redis": [
        "redis>=5.0.0",
    ],
    "diagram": [
        "termaid>=0.1; python_version >= '3.11'",
    ],
}
PACKAGE_DATA = {
    "iac_code": [
        "**/*.yml",
        "**/*.yaml",
        "**/*.json",
        "**/*.md",
        "**/*.rego",
        "**/*.mo",
        "**/*.po",
    ],
}


def _read_version():
    content = INIT_PATH.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', content, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("unable to read package version from %s" % INIT_PATH)
    return match.group(1)


def _read_long_description():
    readme = Path("README.md")
    return readme.read_text(encoding="utf-8") if readme.is_file() else ""


def _try_import_babel():
    """Try importing babel, return (read_po, write_mo) or None."""
    try:
        from babel.messages.mofile import write_mo
        from babel.messages.pofile import read_po

        return read_po, write_mo
    except ImportError:
        return None


def _run(cmd):
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ensure_babel():
    """Import babel, trying every available install method. Raise on total failure."""
    result = _try_import_babel()
    if result:
        return result

    attempts = []

    # 1) pip install babel
    try:
        _run([sys.executable, "-m", "pip", "install", "babel"])
        result = _try_import_babel()
        if result:
            return result
    except Exception as exc:
        attempts.append("pip install babel -> %s" % exc)

    # 2) ensurepip + pip install babel
    try:
        _run([sys.executable, "-m", "ensurepip", "--default-pip"])
        _run([sys.executable, "-m", "pip", "install", "babel"])
        result = _try_import_babel()
        if result:
            return result
    except Exception as exc:
        attempts.append("ensurepip + pip -> %s" % exc)

    # 3) apt-get install python3-babel on Linux builders
    if sys.platform == "linux":
        try:
            _run(["apt-get", "update", "-qq"])
            _run(["apt-get", "install", "-y", "-qq", "python3-babel"])
            result = _try_import_babel()
            if result:
                return result
        except Exception as exc:
            attempts.append("apt-get install python3-babel -> %s" % exc)

    # 4) download get-pip.py, bootstrap pip, then pip install babel
    try:
        import os
        import tempfile

        try:
            from urllib.request import urlretrieve
        except ImportError:
            from urllib import urlretrieve

        fd, get_pip = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
        _run([sys.executable, get_pip, "--break-system-packages", "--quiet"])
        os.remove(get_pip)
        _run([sys.executable, "-m", "pip", "install", "babel"])
        result = _try_import_babel()
        if result:
            return result
    except Exception as exc:
        attempts.append("get-pip.py + pip -> %s" % exc)

    raise RuntimeError(
        "babel is required to compile translations. All install methods failed:\n  " + "\n  ".join(attempts)
    )


def _compile_translations():
    """Compile .po -> .mo for all locales."""
    if not LOCALES_DIR.exists():
        raise RuntimeError("locales directory not found: %s" % LOCALES_DIR)
    po_files = sorted(LOCALES_DIR.rglob("*.po"))
    if not po_files:
        raise RuntimeError("no .po files found under %s" % LOCALES_DIR)
    read_po, write_mo = _ensure_babel()
    for po_file in po_files:
        mo_file = po_file.with_suffix(".mo")
        with open(po_file, "rb") as f:
            catalog = read_po(f)
        with open(mo_file, "wb") as f:
            write_mo(f, catalog)
        print("compiled %s -> %s" % (po_file, mo_file))


def _replace_release_date():
    if platform.system() == 'Darwin':
        return
    content = INIT_PATH.read_text(encoding="utf-8")
    today = date.today().isoformat()
    content = re.sub(
        r'^__release_date__\s*=\s*".*"',
        f'__release_date__ = "{today}"',
        content,
        flags=re.MULTILINE,
    )
    INIT_PATH.write_text(content, encoding="utf-8")


def _copy_selling_skill_references_to_package_root(package_root) -> None:
    """Expand selling-skill reference symlinks into real dirs under an iac_code package root."""
    if not IAC_ALIYUN_REFERENCES_DIR.is_dir():
        raise RuntimeError("references directory not found: %s" % IAC_ALIYUN_REFERENCES_DIR)

    package_root = Path(package_root)
    for skill_name in SELLING_IAC_ALIYUN_SKILLS:
        target = package_root / "pipeline" / "selling" / "skills" / skill_name / "references"
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(IAC_ALIYUN_REFERENCES_DIR, target)


def _copy_selling_skill_references(build_lib: str) -> None:
    """Expand selling-skill reference symlinks into real dirs for installed artifacts."""
    _copy_selling_skill_references_to_package_root(Path(build_lib) / "iac_code")


def _copy_selling_skill_references_to_sdist_release_tree(base_dir: str) -> None:
    """Expand selling-skill references inside an sdist release tree."""
    _copy_selling_skill_references_to_package_root(Path(base_dir) / "src" / "iac_code")


class InjectReleaseDateBuildPy(build_py):
    """Override build_py to stamp __release_date__ before copying source files."""

    def run(self):
        _replace_release_date()
        _compile_translations()
        super().run()
        _copy_selling_skill_references(self.build_lib)


class CompileTranslationsSdist(sdist):
    """Override sdist to compile translations before packaging source."""

    def run(self):
        _replace_release_date()
        _compile_translations()
        super().run()

    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        _copy_selling_skill_references_to_sdist_release_tree(base_dir)


setup(
    name="iac_code",
    version=_read_version(),
    description="Your AI-powered Infrastructure as Code assistant",
    long_description=_read_long_description(),
    long_description_content_type="text/markdown",
    python_requires=">=3.10",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    ],
    packages=find_namespace_packages(where="src"),
    package_dir={"": "src"},
    include_package_data=True,
    package_data=PACKAGE_DATA,
    install_requires=INSTALL_REQUIRES,
    extras_require=EXTRAS_REQUIRE,
    entry_points={"console_scripts": ["iac-code=iac_code.cli.main:app"]},
    cmdclass={
        "build_py": InjectReleaseDateBuildPy,
        "sdist": CompileTranslationsSdist,
    }
)
