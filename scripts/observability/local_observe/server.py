from __future__ import annotations

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scripts.observability.local_observe.assertions import evaluate_assertions
from scripts.observability.local_observe.otlp_decode import DecodeError, decode_logs, decode_metrics, decode_traces
from scripts.observability.local_observe.pipeline_model import build_pipeline_model
from scripts.observability.local_observe.records import new_record
from scripts.observability.local_observe.sample_data import sample_records
from scripts.observability.local_observe.store import ObserveStore


class LocalObserveServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], *, store: ObserveStore, web_dir: Path | None = None) -> None:
        self.store = store
        self.web_dir = web_dir or Path(__file__).parent / "web"
        super().__init__(server_address, LocalObserveHandler)


class LocalObserveHandler(BaseHTTPRequestHandler):
    server: LocalObserveServer

    def do_POST(self) -> None:
        path = self._path()
        if path == "/api/clear":
            self.server.store.clear()
            self._json({"ok": True})
            return
        if path == "/api/demo":
            raw_content = self._query().get("raw_content", ["off"])[0] == "on"
            records = sample_records(raw_content=raw_content)
            self.server.store.clear()
            self.server.store.append_many(records)
            self._json({"ok": True, "record_count": len(records)})
            return

        decoders: dict[str, Callable[[bytes], list[dict]]] = {
            "/v1/traces": decode_traces,
            "/v1/logs": decode_logs,
            "/v1/metrics": decode_metrics,
        }
        decoder = decoders.get(path)
        if decoder is None:
            self._json({"error": "not_found", "message": path}, status=404)
            return
        if "protobuf" not in self.headers.get("Content-Type", ""):
            self._json({"error": "unsupported_content_type"}, status=415)
            return

        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            records = decoder(body)
        except DecodeError as exc:
            self.server.store.append_many(
                [
                    new_record(
                        "error",
                        name="decode_error",
                        attributes={"path": path, "message": str(exc)},
                    )
                ]
            )
            self._json({"error": "decode_error", "message": str(exc)}, status=400)
            return
        self.server.store.append_many(records)
        self._json({"ok": True, "record_count": len(records)})

    def do_GET(self) -> None:
        path = self._path()
        if path == "/api/health":
            self._json(self.server.store.health())
            return
        if path == "/api/snapshot":
            records = self.server.store.records()
            expected_raw_content = self._query().get("expected_raw_content", ["off"])[0]
            record_limit = self._positive_int_query("record_limit")
            visible_records = records[-record_limit:] if record_limit is not None else records
            self._json(
                {
                    "health": self.server.store.health(),
                    "records": visible_records,
                    "pipeline": build_pipeline_model(records),
                    "assertions": evaluate_assertions(records, expected_raw_content=expected_raw_content),
                }
            )
            return
        if path == "/api/export":
            self._text(self.server.store.export_text(), content_type="application/jsonl")
            return
        self._static(path)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _path(self) -> str:
        return urlparse(self.path).path

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _positive_int_query(self, name: str) -> int | None:
        raw = self._query().get(name, [""])[0]
        if not raw.isdigit():
            return None
        value = int(raw)
        return value if value > 0 else None

    def _static(self, request_path: str) -> None:
        target = "index.html" if request_path in {"/", ""} else request_path.lstrip("/")
        web_root = self.server.web_dir.resolve()
        path = (self.server.web_dir / target).resolve()
        try:
            path.relative_to(web_root)
        except ValueError:
            self._text("Not found", status=404)
            return
        if not path.exists() or not path.is_file():
            self._text("Not found", status=404)
            return
        if path.suffix == ".html":
            content_type = "text/html"
        elif path.suffix == ".css":
            content_type = "text/css"
        else:
            content_type = "text/javascript"
        self._text(path.read_text(encoding="utf-8"), content_type=content_type)

    def _json(self, data: dict, *, status: int = 200) -> None:
        self._text(json.dumps(data, ensure_ascii=False, default=str), status=status, content_type="application/json")

    def _text(self, text: str, *, status: int = 200, content_type: str = "text/plain") -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
