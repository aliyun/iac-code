"""Custom build hook to inject __release_date__ at build time."""

import platform
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

INIT_PATH = Path("src/iac_code/__init__.py")
LOCALES_DIR = Path("src/iac_code/i18n/locales")


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

    # 2) ensurepip + pip install babel (Debian strips ensurepip into python3-venv)
    try:
        _run([sys.executable, "-m", "ensurepip", "--default-pip"])
        _run([sys.executable, "-m", "pip", "install", "babel"])
        result = _try_import_babel()
        if result:
            return result
    except Exception as exc:
        attempts.append("ensurepip + pip -> %s" % exc)

    # 3) apt-get install python3-babel (Debian/Ubuntu container with root) — Linux only
    if sys.platform == "linux":
        try:
            _run(["apt-get", "update", "-qq"])
            _run(["apt-get", "install", "-y", "-qq", "python3-babel"])
            result = _try_import_babel()
            if result:
                return result
        except Exception as exc:
            attempts.append("apt-get install python3-babel -> %s" % exc)

    # 4) download get-pip.py via urllib, bootstrap pip, then pip install babel
    try:
        import tempfile
        try:
            from urllib.request import urlretrieve
        except ImportError:
            from urllib import urlretrieve
        fd, get_pip = tempfile.mkstemp(suffix=".py")
        import os
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
        "babel is required to compile translations. All install methods failed:\n  "
        + "\n  ".join(attempts)
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


class InjectReleaseDateBuildPy(build_py):
    """Override build_py to stamp __release_date__ before copying source files."""

    def run(self):
        _replace_release_date()
        _compile_translations()
        super().run()


class CompileTranslationsSdist(sdist):
    """Override sdist to compile translations before packaging source."""

    def run(self):
        _replace_release_date()
        _compile_translations()
        super().run()


setup(
    cmdclass={
        "build_py": InjectReleaseDateBuildPy,
        "sdist": CompileTranslationsSdist,
    }
)
