"""Load public, process-shared resources before a warm-pool server is ready."""

from __future__ import annotations

import os


def warmup_enabled() -> bool:
    return os.environ.get("IAC_CODE_A2A_STARTUP_WARMUP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def warmup_runtime() -> None:
    # Imports only: no providers/HTTP clients, credentials, or sessions are created.
    from iac_code.pipeline import discover_pipelines
    from iac_code.providers import manager  # noqa: F401
    from iac_code.skills.bundled import init_bundled_skills
    from iac_code.tools.cloud.aliyun import endpoint_resolver, openmeta
    from iac_code.utils.bundled_data import load_bundled_data

    init_bundled_skills()
    openmeta.load_openmeta_exclusions()
    for path in (endpoint_resolver._CATALOG_PATH, endpoint_resolver._UNAVAILABLE_PATH):
        load_bundled_data(path, format="json")
    load_bundled_data(endpoint_resolver._OVERRIDES_PATH, format="yaml")
    for directory in discover_pipelines().values():
        load_bundled_data(directory / "pipeline.yaml", format="yaml")
