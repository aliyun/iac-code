"""Shared User-Agent builder for Alibaba Cloud OpenAPI clients.

Format:
    iac-code/<version>+<release_date_or_dev> (<os>; <arch>; Python/<py_ver>)

Empty ``__release_date__`` renders as ``+dev`` so server-side logs can
distinguish unpackaged local runs from released builds.
"""

from __future__ import annotations

import platform


def build_user_agent() -> str:
    from iac_code import __release_date__, __version__

    system = platform.system()
    os_name = "macOS" if system == "Darwin" else (system or "unknown")
    arch = platform.machine() or "unknown"
    py_ver = platform.python_version()
    build = __release_date__.strip() or "dev"
    return f"iac-code/{__version__}+{build} ({os_name}; {arch}; Python/{py_ver})"
