"""A non-cloud pause for scoped workers, not a resource or model deadline."""

import time
from pathlib import Path
from typing import Any

from .files import WorkflowError, digest

LOADED_SHA = digest(Path(__file__))


def worker_wait(seconds: Any = 30) -> dict[str, Any]:
    """Yield one HTTP request without holding a browser or database lock.

    Workers can repeat this while awaiting the original answer/cloud operation.
    The per-call bound keeps transport requests responsive; it neither limits
    the overall resource run nor consumes or renews any API allowance.
    """
    if type(seconds) is not int or not 1 <= seconds <= 30:
        raise WorkflowError("wait seconds must be an integer from 1 to 30; no resource deadline")
    started = time.monotonic()
    time.sleep(seconds)
    return {"waited_seconds": time.monotonic() - started, "cloud_calls": 0, "resource_deadline": None}
