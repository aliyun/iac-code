"""Custom build hook to inject __release_date__ at build time."""

import re
from datetime import date
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

INIT_PATH = Path("src/iac_code/__init__.py")


class InjectReleaseDateBuildPy(build_py):
    """Override build_py to stamp __release_date__ before copying source files."""

    def run(self):
        content = INIT_PATH.read_text(encoding="utf-8")
        today = date.today().isoformat()
        content = re.sub(
            r'^__release_date__\s*=\s*".*"',
            f'__release_date__ = "{today}"',
            content,
            flags=re.MULTILINE,
        )
        INIT_PATH.write_text(content, encoding="utf-8")
        super().run()


setup(cmdclass={"build_py": InjectReleaseDateBuildPy})
