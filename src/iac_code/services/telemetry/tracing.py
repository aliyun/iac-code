"""SpanFactory — wraps the OTel Tracer."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context, Token
from opentelemetry.trace import Tracer

_TRACER_NAME = "com.iac-code.tracing"
_TRACER_VERSION = "1.0.0"


class SpanFactory:
    """Context-manager span producer."""

    def __init__(self) -> None:
        self._tracer: Tracer | None = None

    def attach(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def _get_tracer(self) -> Tracer:
        if self._tracer is None:
            return trace.get_tracer(_TRACER_NAME, _TRACER_VERSION)
        return self._tracer

    @contextmanager
    def start(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """Start a span as a context manager."""
        tracer = self._get_tracer()
        with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            yield span
            attrs = getattr(span, "attributes", None)
            if attrs:
                logger.debug("[span] {} {}", name, {k: v for k, v in attrs.items()})

    def start_detached(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        parent_context: Context | None = None,
    ) -> Any:
        """Start a span without changing the current OTel context."""
        return self._get_tracer().start_span(
            name,
            context=parent_context,
            attributes=attributes or {},
        )

    @staticmethod
    def use(
        span: Any,
        *,
        record_exception: bool = False,
        set_status_on_exception: bool = False,
        end_on_exit: bool = False,
    ):
        """Temporarily activate an existing span without owning its lifetime."""
        return trace.use_span(
            span,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
            end_on_exit=end_on_exit,
        )


def get_current_context() -> Context:
    return otel_context.get_current()


def attach_context(context: Context) -> Token[Context]:
    return otel_context.attach(context)


def detach_context(token: Token[Context]) -> None:
    otel_context.detach(token)
