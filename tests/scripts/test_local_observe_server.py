import json
from http.client import HTTPConnection
from threading import Thread

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from scripts.observability.local_observe.server import LocalObserveServer
from scripts.observability.local_observe.store import ObserveStore


def _start_server(tmp_path):
    store = ObserveStore(data_dir=tmp_path, memory_limit=100)
    server = LocalObserveServer(("127.0.0.1", 0), store=store)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store


def _start_server_with_web_dir(tmp_path, web_dir):
    store = ObserveStore(data_dir=tmp_path / "data", memory_limit=100)
    server = LocalObserveServer(("127.0.0.1", 0), store=store, web_dir=web_dir)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store


def test_trace_ingest_and_snapshot(tmp_path):
    server, store = _start_server(tmp_path)
    try:
        req = ExportTraceServiceRequest(
            resource_spans=[
                ResourceSpans(scope_spans=[ScopeSpans(spans=[Span(name="iac.pipeline.step", span_id=b"12345678")])])
            ]
        )
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/v1/traces",
            body=req.SerializeToString(),
            headers={"Content-Type": "application/x-protobuf"},
        )
        response = conn.getresponse()
        assert response.status == 200
        assert store.records()[0]["name"] == "iac.pipeline.step"

        conn.request("GET", "/api/snapshot")
        snapshot_response = conn.getresponse()
        assert snapshot_response.status == 200
        body = snapshot_response.read().decode("utf-8")
        assert "iac.pipeline.step" in body
    finally:
        server.shutdown()


def test_bad_protobuf_returns_400_and_error_record(tmp_path):
    server, store = _start_server(tmp_path)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/v1/traces", body=b"not protobuf", headers={"Content-Type": "application/x-protobuf"})
        response = conn.getresponse()
        assert response.status == 400
        assert store.records()[0]["kind"] == "error"
    finally:
        server.shutdown()


def test_snapshot_uses_expected_raw_content_query(tmp_path):
    server, store = _start_server(tmp_path)
    store.append_many(
        [
            {
                "id": "span_1",
                "kind": "span",
                "name": "enter_ai_application_system",
                "attributes": {},
            }
        ]
    )
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/api/snapshot?expected_raw_content=on")
        response = conn.getresponse()
        assert response.status == 200

        snapshot = json.loads(response.read().decode("utf-8"))
        raw_input_assertion = next(
            item for item in snapshot["assertions"] if item["label"] == "gen_ai.input.messages present"
        )
        assert raw_input_assertion["status"] == "fail"
    finally:
        server.shutdown()


def test_snapshot_limits_records_payload_but_models_all_records(tmp_path):
    server, store = _start_server(tmp_path)
    store.append_many(
        [
            {
                "id": "step_span",
                "kind": "span",
                "name": "iac.pipeline.step",
                "span_id": "s_step",
                "attributes": {
                    "pipeline_name": "selling",
                    "session_id": "sess",
                    "step_id": "template",
                    "step_attempt": 1,
                },
            },
            {
                "id": "raw_1",
                "kind": "log",
                "name": "debug.one",
                "attributes": {"pipeline_name": "selling", "session_id": "sess"},
            },
            {
                "id": "raw_2",
                "kind": "log",
                "name": "debug.two",
                "attributes": {"pipeline_name": "selling", "session_id": "sess"},
            },
        ]
    )
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/api/snapshot?record_limit=2")
        response = conn.getresponse()
        assert response.status == 200

        snapshot = json.loads(response.read().decode("utf-8"))
        assert [record["id"] for record in snapshot["records"]] == ["raw_1", "raw_2"]
        assert snapshot["health"]["record_count"] == 3
        run = snapshot["pipeline"]["runs"][0]
        assert run["record_ids"] == ["step_span", "raw_1", "raw_2"]
        assert run["steps"][0]["record_ids"] == ["step_span"]
        assert run["steps"][0]["evidence_records"][0]["id"] == "step_span"
        assert [record["id"] for record in run["evidence_records"]] == ["raw_1", "raw_2"]
    finally:
        server.shutdown()


def test_demo_endpoint_can_seed_debug_on_records(tmp_path):
    server, store = _start_server(tmp_path)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/demo?raw_content=on")
        response = conn.getresponse()
        assert response.status == 200
        response.read()

        records = store.records()
        assert any(record["attributes"].get("gen_ai.input.messages") for record in records)
        assert any(record["attributes"].get("gen_ai.tool.call.arguments") for record in records)
        assert any(record["attributes"].get("gen_ai.tool.call.result") for record in records)

        conn.request("GET", "/api/snapshot?expected_raw_content=on")
        snapshot_response = conn.getresponse()
        snapshot = json.loads(snapshot_response.read().decode("utf-8"))
        raw_input_assertion = next(
            item for item in snapshot["assertions"] if item["label"] == "gen_ai.input.messages present"
        )
        assert raw_input_assertion["status"] == "pass"
        tool_child = next(
            child
            for run in snapshot["pipeline"]["runs"]
            for step in run["steps"]
            for round_record in step["agent_rounds"]
            for child in round_record["children"]
            if child["record_id"] == "demo_tool_1"
        )
        assert tool_child["attributes"]["gen_ai.tool.call.arguments"]
        assert tool_child["attributes"]["gen_ai.tool.call.result"]
    finally:
        server.shutdown()


def test_demo_endpoint_can_seed_debug_off_records(tmp_path):
    server, store = _start_server(tmp_path)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/demo?raw_content=off")
        response = conn.getresponse()
        assert response.status == 200
        response.read()

        records = store.records()
        assert records
        assert all("gen_ai.input.messages" not in record["attributes"] for record in records)
        assert all("gen_ai.tool.call.arguments" not in record["attributes"] for record in records)
        assert all("gen_ai.tool.call.result" not in record["attributes"] for record in records)

        conn.request("GET", "/api/snapshot?expected_raw_content=off")
        snapshot_response = conn.getresponse()
        snapshot = json.loads(snapshot_response.read().decode("utf-8"))
        raw_input_assertion = next(
            item for item in snapshot["assertions"] if item["label"] == "gen_ai.input.messages absent"
        )
        assert raw_input_assertion["status"] == "pass"
    finally:
        server.shutdown()


def test_static_files_cannot_escape_web_dir(tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("index", encoding="utf-8")
    sibling_dir = tmp_path / "web_secret"
    sibling_dir.mkdir()
    (sibling_dir / "leak.txt").write_text("leaked", encoding="utf-8")
    server, _store = _start_server_with_web_dir(tmp_path, web_dir)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/../web_secret/leak.txt")
        response = conn.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 404
        assert body != "leaked"
    finally:
        server.shutdown()
