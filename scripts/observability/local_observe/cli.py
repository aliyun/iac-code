from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from scripts.observability.local_observe.server import LocalObserveServer
from scripts.observability.local_observe.store import ObserveStore


def env_lines(url: str) -> list[str]:
    return [
        f"export IAC_CODE_TELEMETRY_ENDPOINT={url}",
        "export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1",
        "export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local OTLP observe server for iac-code development.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", type=Path, default=Path(".local-observe"))
    parser.add_argument("--memory-limit", type=int, default=5000)
    parser.add_argument("--no-open", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ObserveStore(data_dir=args.data_dir, memory_limit=args.memory_limit)
    server = LocalObserveServer((args.host, args.port), store=store)
    url = f"http://{args.host}:{server.server_port}"
    print(f"Web UI: {url}")
    print(f"OTLP endpoint: {url}")
    print("Env:")
    for line in env_lines(url):
        print(f"  {line}")
    print("No raw content variant:")
    print(f"  export IAC_CODE_TELEMETRY_ENDPOINT={url}")
    print("  export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1")
    print("  unset OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local observe server")
    finally:
        server.server_close()
