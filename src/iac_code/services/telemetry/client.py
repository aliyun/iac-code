"""TelemetryClient — top-level facade that wires all components."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from iac_code.config import get_config_dir, get_settings_path
from iac_code.services.telemetry.attributes import AttributeBuilder
from iac_code.services.telemetry.config import is_telemetry_disabled
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.fallback import FallbackStore
from iac_code.services.telemetry.identity import Identity
from iac_code.services.telemetry.metrics import MetricsRegistry
from iac_code.services.telemetry.names import Events
from iac_code.services.telemetry.sink import AnalyticsSink
from iac_code.services.telemetry.tracing import SpanFactory

log = logging.getLogger(__name__)

_METRICS_EXPORT_INTERVAL_MS = 60_000
_EVENTS_BATCH_DELAY_MS = 10_000
_EVENTS_BATCH_MAX_SIZE = 200
_FLUSH_TIMEOUT_MS = 2_000

_SERVICE_NAME = "iac-code"


class TelemetryClient:
    """Top-level facade. Assembles all components and manages lifecycle."""

    # ------------------------------------------------------------------
    # Built-in default backend configuration
    # ------------------------------------------------------------------
    # Initial target: Aliyun ARMS (APM tracing). Users override via
    # IAC_CODE_TELEMETRY_* environment variables. Each signal has its own
    # endpoint because ARMS uses different URL paths per signal (unlike
    # standard OTLP which uses a single base URL).
    #
    # To switch to a different backend, update these class constants (or
    # have your release pipeline inject them). The code itself is
    # backend-agnostic — it just speaks OTLP/HTTP.
    _DEFAULT_TRACES_ENDPOINT_FALLBACK = (
        "https://proj-xtrace-376253ca1b7e89bdb77df391878f64-cn-beijing"
        ".cn-beijing.log.aliyuncs.com/apm/trace/opentelemetry/v1/traces"
    )
    _DEFAULT_METRICS_ENDPOINT_FALLBACK = ""  # ARMS metrics OTLP endpoint TBD
    _DEFAULT_LOGS_ENDPOINT_FALLBACK = ""  # ARMS logs OTLP endpoint TBD
    _DEFAULT_HEADERS_FALLBACK = (
        "x-arms-license-key=dqlxgky1hg@5f3b9e3371b5487,"
        "x-arms-project=proj-xtrace-376253ca1b7e89bdb77df391878f64-cn-beijing,"
        "x-cms-workspace=iac-code-cli"
    )

    def __init__(
        self,
        *,
        session_id: str | None = None,
        identity: Identity | None = None,
        attributes: AttributeBuilder | None = None,
        metrics: MetricsRegistry | None = None,
        events: EventEmitter | None = None,
        tracer: SpanFactory | None = None,
        sink: AnalyticsSink | None = None,
        fallback: FallbackStore | None = None,
        settings_path: Path | None = None,
        telemetry_dir: Path | None = None,
    ) -> None:
        # Default-wire every component that wasn't injected.
        self._identity = identity or Identity(settings_path or get_settings_path(), session_id=session_id)
        self._attributes = attributes or AttributeBuilder(self._identity, _SERVICE_NAME)
        self._metrics = metrics or MetricsRegistry()
        self._events = events or EventEmitter(self._attributes)
        self._tracer = tracer or SpanFactory()
        self._sink = sink or AnalyticsSink(self._events)
        self._fallback = fallback or FallbackStore(telemetry_dir or (get_config_dir() / "telemetry"))

        self._meter_provider: MeterProvider | None = None
        self._logger_provider: LoggerProvider | None = None
        self._tracer_provider: TracerProvider | None = None
        self._bootstrapped = False

    # -------- Public API --------

    def log_event(self, event_name: str, metadata: dict[str, Any] | None = None) -> None:
        self._sink.log_event(event_name, metadata or {})

    def add_metric(self, name: str, value: int | float, attrs: dict[str, Any] | None = None) -> None:
        self._metrics.add(name, value, attrs or {})

    def start_span(self, name: str, attrs: dict[str, Any] | None = None):
        return self._tracer.start(name, attrs)

    def get_session_id(self) -> str:
        return self._identity.get_session_id()

    def get_user_id(self) -> str:
        return self._identity.get_user_id()

    # -------- Lifecycle --------

    def bootstrap(self) -> None:
        """Wire OTel SDK, activate sink, replay pre-queue, retry failed batches.

        Idempotent.
        """
        if self._bootstrapped:
            return
        self._bootstrapped = True

        resource = Resource.create(self._attributes.build_resource())

        # MeterProvider
        readers: list = []
        if self._default_metrics_enabled():
            readers.append(
                PeriodicExportingMetricReader(
                    self._build_default_metric_exporter(),
                    export_interval_millis=_METRICS_EXPORT_INTERVAL_MS,
                )
            )
        self._maybe_append_user_metric_reader(readers)
        self._meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        self._metrics.register_all(self._meter_provider.get_meter("com.iac-code.metrics", "1.0.0"))

        # LoggerProvider (Events)
        lp = LoggerProvider(resource=resource)
        if self._default_logs_enabled():
            lp.add_log_record_processor(
                BatchLogRecordProcessor(
                    self._build_default_log_exporter(),
                    schedule_delay_millis=_EVENTS_BATCH_DELAY_MS,
                    max_export_batch_size=_EVENTS_BATCH_MAX_SIZE,
                )
            )
        self._maybe_append_user_log_processor(lp)
        self._logger_provider = lp
        set_logger_provider(lp)
        self._events.attach(lp.get_logger("com.iac-code.events", "1.0.0"))

        # TracerProvider
        tp = TracerProvider(resource=resource)
        if self._default_traces_enabled():
            tp.add_span_processor(BatchSpanProcessor(self._build_default_span_exporter()))
        self._maybe_append_user_span_processor(tp)
        self._tracer_provider = tp
        self._tracer.attach(tp.get_tracer("com.iac-code.tracing", "1.0.0"))

        # Activate sink + drain pre-queue
        self._sink.activate()
        self._sink.drain_soon()

        # iac.init once per lifetime
        if self._identity.was_first_run():
            self.log_event(Events.INIT, {"is_first_run": True})

        # Retry previously failed batches
        self._retry_failed_batches()

    def flush(self, timeout_ms: int = _FLUSH_TIMEOUT_MS) -> None:
        """Force-flush providers without closing them; never raise.

        Use this between units of work (e.g. per-task in a2a/acp servers) to
        push pending batches before the runtime can be killed. Unlike
        ``shutdown()``, the providers stay usable for subsequent work.
        """
        for provider, label in (
            (self._meter_provider, "MeterProvider"),
            (self._logger_provider, "LoggerProvider"),
            (self._tracer_provider, "TracerProvider"),
        ):
            if provider is None:
                continue
            self._safe_force_flush(provider, label, timeout_ms)

    def shutdown(self) -> None:
        """Force-flush providers with bounded timeout; never raise."""
        for provider, label in (
            (self._meter_provider, "MeterProvider"),
            (self._logger_provider, "LoggerProvider"),
            (self._tracer_provider, "TracerProvider"),
        ):
            if provider is None:
                continue
            self._safe_flush(provider, label)

    # -------- Default OTLP backend wiring helpers --------

    @classmethod
    def _traces_endpoint(cls) -> str:
        """Full URL for traces export. Per-signal override > base + suffix > fallback."""
        override = os.environ.get("IAC_CODE_TELEMETRY_TRACES_ENDPOINT", "").strip()
        if override:
            return override
        base = os.environ.get("IAC_CODE_TELEMETRY_ENDPOINT", "").strip()
        if base:
            return f"{base}/v1/traces"
        return cls._DEFAULT_TRACES_ENDPOINT_FALLBACK

    @classmethod
    def _metrics_endpoint(cls) -> str:
        override = os.environ.get("IAC_CODE_TELEMETRY_METRICS_ENDPOINT", "").strip()
        if override:
            return override
        base = os.environ.get("IAC_CODE_TELEMETRY_ENDPOINT", "").strip()
        if base:
            return f"{base}/v1/metrics"
        return cls._DEFAULT_METRICS_ENDPOINT_FALLBACK

    @classmethod
    def _logs_endpoint(cls) -> str:
        override = os.environ.get("IAC_CODE_TELEMETRY_LOGS_ENDPOINT", "").strip()
        if override:
            return override
        base = os.environ.get("IAC_CODE_TELEMETRY_ENDPOINT", "").strip()
        if base:
            return f"{base}/v1/logs"
        return cls._DEFAULT_LOGS_ENDPOINT_FALLBACK

    @classmethod
    def _default_headers(cls) -> dict[str, str]:
        """Parse 'k1=v1,k2=v2' format (matches OTEL_EXPORTER_OTLP_HEADERS)."""
        raw = os.environ.get("IAC_CODE_TELEMETRY_HEADERS", cls._DEFAULT_HEADERS_FALLBACK)
        headers: dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            headers[k.strip()] = v.strip()
        return headers

    @classmethod
    def _default_traces_enabled(cls) -> bool:
        return bool(cls._traces_endpoint()) and not is_telemetry_disabled()

    @classmethod
    def _default_metrics_enabled(cls) -> bool:
        return bool(cls._metrics_endpoint()) and not is_telemetry_disabled()

    @classmethod
    def _default_logs_enabled(cls) -> bool:
        return bool(cls._logs_endpoint()) and not is_telemetry_disabled()

    @classmethod
    def _build_default_metric_exporter(cls) -> OTLPMetricExporter:
        return OTLPMetricExporter(
            endpoint=cls._metrics_endpoint(),
            headers=cls._default_headers(),
            timeout=5,
        )

    @classmethod
    def _build_default_log_exporter(cls) -> OTLPLogExporter:
        return OTLPLogExporter(
            endpoint=cls._logs_endpoint(),
            headers=cls._default_headers(),
            timeout=10,
        )

    @classmethod
    def _build_default_span_exporter(cls) -> OTLPSpanExporter:
        return OTLPSpanExporter(
            endpoint=cls._traces_endpoint(),
            headers=cls._default_headers(),
            timeout=10,
        )

    @staticmethod
    def _user_otlp_enabled() -> bool:
        return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))

    def _maybe_append_user_metric_reader(self, readers: list) -> None:
        if not self._user_otlp_enabled():
            return
        try:
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(),  # reads env
                    export_interval_millis=_METRICS_EXPORT_INTERVAL_MS,
                )
            )
        except Exception as e:
            log.warning("Failed to wire user OTLP metric exporter: %s", e)

    def _maybe_append_user_log_processor(self, provider: LoggerProvider) -> None:
        if not self._user_otlp_enabled():
            return
        try:
            provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(),
                    schedule_delay_millis=_EVENTS_BATCH_DELAY_MS,
                    max_export_batch_size=_EVENTS_BATCH_MAX_SIZE,
                )
            )
        except Exception as e:
            log.warning("Failed to wire user OTLP log exporter: %s", e)

    def _maybe_append_user_span_processor(self, provider: TracerProvider) -> None:
        if not self._user_otlp_enabled():
            return
        try:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception as e:
            log.warning("Failed to wire user OTLP span exporter: %s", e)

    def _retry_failed_batches(self) -> None:
        """Scan disk for failed batches from previous sessions, re-emit, delete."""
        for path in self._fallback.list_pending():
            try:
                for event in self._fallback.read(path):
                    name = event.pop("event.name", "iac.unknown")
                    self._events.emit(name, event)
                self._fallback.remove(path)
            except Exception as e:
                log.warning("Failed to retry batch %s: %s", path, e)

    @staticmethod
    def _safe_force_flush(provider: object, label: str, timeout_ms: int) -> None:
        flush = getattr(provider, "force_flush", None)
        if flush is None:
            return
        try:
            flush(timeout_ms)
        except Exception as e:
            log.warning("Flush %s failed: %s", label, e)

    @classmethod
    def _safe_flush(cls, provider: object, label: str) -> None:
        cls._safe_force_flush(provider, label, _FLUSH_TIMEOUT_MS)
        shutdown = getattr(provider, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception as e:
                log.warning("Shutdown %s failed: %s", label, e)
