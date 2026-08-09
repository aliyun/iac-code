"""Recognize credential-runtime ECS failures at the Alibaba Cloud tool boundary.

The credential runtime reports every ECS instance RAM role failure as
`ValueError(<stable code>)`. When that failure happens while an SDK client signs a
request, the Darabonba core wraps it into `UnretryableException` and keeps the original
exception in `inner_exception`, so a call site that only looks at the outer exception
loses the stable code and falls back to generic transport text.

The match is deliberately narrow so an unrelated failure is never reported to the user
as a credential problem: only a `ValueError` whose message is exactly one of the stable
codes, or exactly that one SDK envelope directly around such a `ValueError`. A
`RuntimeError` with a code-shaped message, an envelope around one, and anything reachable
only through `__cause__`/`__context__` are all left alone.
"""

from __future__ import annotations

from darabonba.exceptions import UnretryableException

from iac_code.services.providers.aliyun_credentials_runtime import ECS_CREDENTIAL_ERROR_CODES


def _carried_code(error: object) -> str | None:
    if not isinstance(error, ValueError):
        return None
    code = str(error)
    return code if code in ECS_CREDENTIAL_ERROR_CODES else None


def ecs_credential_error_code(error: BaseException) -> str | None:
    """Return the stable ECS credential code carried by `error`, else `None`."""
    code = _carried_code(error)
    if code is not None:
        return code
    # `type(...) is` matches the transport helpers in retry_policy: only the Darabonba
    # envelope counts, never the Tea base class it inherits from.
    if type(error) is UnretryableException:
        return _carried_code(error.inner_exception)
    return None
