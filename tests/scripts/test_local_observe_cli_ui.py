import subprocess
import sys
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

from scripts.observability.local_observe import cli
from scripts.observability.local_observe.server import LocalObserveServer
from scripts.observability.local_observe.store import ObserveStore


def test_env_lines_include_endpoint_and_content_capture():
    lines = cli.env_lines("http://127.0.0.1:4318")

    assert "export IAC_CODE_TELEMETRY_ENDPOINT=http://127.0.0.1:4318" in lines
    assert "export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1" in lines
    assert "export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT" in lines


def test_entrypoint_runs_as_script():
    repo_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "observability" / "local_observe.py"), "--help"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--port" in result.stdout


def test_server_serves_static_web_ui(tmp_path):
    store = ObserveStore(data_dir=tmp_path, memory_limit=100)
    server = LocalObserveServer(("127.0.0.1", 0), store=store)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "Local OTLP Pipeline Test Console" in body
        assert "IAC_CODE_ENABLE_LOCAL_TELEMETRY" in body
        assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in body
        assert "Demo debug off" in body
        assert "Demo debug on" in body
    finally:
        server.shutdown()


def test_static_ui_preserves_drilldown_state_limits_raw_records_and_shows_details():
    repo_root = Path(__file__).resolve().parents[2]
    app_js = (repo_root / "scripts" / "observability" / "local_observe" / "web" / "app.js").read_text(encoding="utf-8")

    assert "openNodes" in app_js
    assert "data-node-id" in app_js
    assert "RAW_RECORD_LIMIT" in app_js
    assert "record_limit" in app_js
    assert "renderRecordDetails" in app_js
    assert "evidence_groups" in app_js
    assert "evidence_records" in app_js
    assert "gen_ai.input.messages" in app_js
    assert "gen_ai.tool.call.arguments" in app_js
    assert "renderUnscopedMetrics" in app_js
    assert "Pipeline lifecycle" in app_js
    assert "Normal chat after pipeline" in app_js
    assert "Other session evidence" in app_js
    assert "Step evidence" in app_js
    assert "Local evidence id" in app_js
    assert "Smoke checks" in app_js
