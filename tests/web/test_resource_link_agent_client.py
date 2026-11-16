"""Script client/transport tests: original bytes, no models or cloud dependencies."""

import base64
import hashlib
import io
import json
import os
import stat
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.web.e2e.resource_link_workflow import agent_client, agent_transport


@pytest.mark.parametrize("image", [False, True])
def test_real_script_http_and_original_image_copy(tmp_path, capsys, image):
    pixels = b"offline-original-pixels"
    sha = hashlib.sha256(pixels).hexdigest()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            assert self.path == "/invoke"
            assert self.headers["Authorization"] == "Bearer fake-job-token"
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            result = (
                {
                    "image_url": "data:image/png;base64," + base64.b64encode(pixels).decode(),
                    "sha256": sha,
                    "evidence_role": "diagnostic_only",
                    "eligible_for_verdict": False,
                }
                if image
                else {
                    "task_id": "R0036",
                    "job_id": "a" * 32,
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "result": result}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / ".workflow-client.json").write_text(
        json.dumps(
            {
                "origin": f"http://127.0.0.1:{server.server_port}",
                "token": "fake-job-token",
            }
        ), encoding="utf-8"
    )
    try:
        assert agent_client.main(["inspect" if image else "context"], home=tmp_path) == 0
        output = capsys.readouterr().out
        assert "fake-job-token" not in output
        result = json.loads(output)
        assert calls == [{"operation": "inspect" if image else "context", "arguments": {}}]
        if image:
            assert "image_url" not in result
            assert result["eligible_for_verdict"] is False
            assert result["inspect_with"] == "view_image"
            target = Path(result["image_path"])
            assert target.read_bytes() == pixels
            assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
            assert stat.S_IMODE(target.stat().st_mode) == (0o666 if os.name == "nt" else 0o600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_client_never_retries_unknown_write(monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError("fixture private failure")
    monkeypatch.setattr(agent_client.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError, match="outcome unknown"):
        agent_client.invoke(
            {"operation": "request", "arguments": {"dimension": "CREATE"}},
            {"origin": "http://127.0.0.1:12345", "token": "fake"},
        )
    assert opener.open.call_count == 1


@pytest.mark.parametrize("phase,exception", [("descriptor", OSError), ("bridge", ValueError)])
def test_client_failure_identifies_safe_phase_without_private_message(tmp_path, monkeypatch, capsys, phase, exception):
    if phase == "bridge":
        (tmp_path / ".workflow-client.json").write_text("{}", encoding="utf-8")

        def fail(*_args):
            raise exception("private token and URL must never be printed")

        monkeypatch.setattr(agent_client, "invoke", fail)
    assert agent_client.main(["context"], home=tmp_path) == 1
    result = capsys.readouterr().out
    assert "private token" not in result and "127.0.0.1" not in result
    value = json.loads(result)
    assert value["phase"] == phase
    assert value["error_type"] in {"FileNotFoundError", "ValueError"}


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "_bind", "arguments": {}},
        {"operation": "context", "arguments": []},
        {"operation": "context", "arguments": {}, "job_id": "other"},
    ],
)
def test_transport_operations_and_override_rejected(payload):
    with pytest.raises(ValueError, match="public operation"):
        agent_client.invoke(payload, {})


def test_image_tampering_rejected_without_creating_file(tmp_path):
    with pytest.raises(ValueError, match="SHA mismatch"):
        agent_client.save_image(
            {"image_url": "data:image/png;base64,cGl4ZWxz", "sha256": "a" * 64},
            tmp_path / "images",
        )
    assert not (tmp_path / "images").exists()


def test_client_argument_file_and_private_local_install(tmp_path, monkeypatch, capsys):
    descriptor = {"origin": "http://127.0.0.1:12345", "token": "fake-job-only"}
    result = agent_transport.install_local(tmp_path / "sandbox", "# fixture\n", descriptor)
    assert result["script_sha256"] == hashlib.sha256(b"# fixture\n").hexdigest()
    for target in (tmp_path / "sandbox").iterdir():
        assert stat.S_IMODE(target.stat().st_mode) == (0o666 if os.name == "nt" else 0o600)
    argument_file = tmp_path / "arguments.json"
    argument_file.write_text(json.dumps({"report": "原始证据"}), encoding="utf-8")
    calls = []

    def invoke(request, actual):
        assert actual == descriptor
        calls.append(request)
        return {"ok": False, "outcome": "rejected", "error": "explicit gate rejection"}

    monkeypatch.setattr(agent_client, "invoke", invoke)
    assert agent_client.main(["submit", "--arguments-file", str(argument_file)], home=tmp_path / "sandbox") == 1
    assert calls == [{"operation": "submit", "arguments": {"report": "原始证据"}}]
    assert json.loads(capsys.readouterr().out)["outcome"] == "rejected"


@pytest.mark.skipif(os.name == "nt", reason="SSH OPS host is macOS/Linux, not Windows")
@pytest.mark.parametrize("conflict", [False, True])
def test_remote_install_has_only_job_client_and_lifetime_forward(tmp_path, monkeypatch, capsys, conflict):
    job_id = "b" * 32
    directory = tmp_path / "jobs" / job_id
    directory.mkdir(parents=True)
    (tmp_path / "environment.json").write_text("{}", encoding="utf-8")
    remote_cwd = "/remote/workers/" + job_id
    (directory / "launch.json").write_text(
        json.dumps({"worker_backend": {"host": "macmini", "cwd": remote_cwd}}), encoding="utf-8"
    )
    descriptor = {"origin": "http://127.0.0.1:23456", "token": "fake-per-job-bridge"}
    monkeypatch.setattr(agent_transport.sys, "argv", ["helper", str(tmp_path), job_id, "23456"])
    monkeypatch.setattr(agent_transport.sys, "stdin", io.StringIO(json.dumps(descriptor) + "\n"))
    monkeypatch.setattr(
        agent_transport.worker_hosts,
        "ssh_base",
        lambda config: ["ssh", "-o", "pinned", "fixture@host"],
    )
    monkeypatch.setattr(agent_transport.worker_hosts, "mini", lambda config: {"python": "/usr/bin/python3"})
    monkeypatch.setattr(agent_transport.select, "select", lambda readable, *args: (readable, [], []))
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"Allocated port 35267 for remote forward to 127.0.0.1:23456\n")
    os.close(write_fd)
    tunnel = Mock()
    tunnel.stderr = os.fdopen(read_fd, "rb")
    tunnel.poll.return_value = None
    launches = []

    def popen(argv, **kwargs):
        launches.append(argv)
        return tunnel

    def run(argv, **kwargs):
        request = json.loads(kwargs["input"])
        assert request["cwd"] == remote_cwd
        assert set(request) == {"cwd", "script", "descriptor"}
        assert request["descriptor"] == {**descriptor, "origin": "http://127.0.0.1:35267"}
        assert "server.json" not in request["script"]
        source_sha = hashlib.sha256(request["script"].encode()).hexdigest()
        return Mock(
            stdout=json.dumps(
                {
                    "ready": True,
                    "script": remote_cwd + "/workflow-agent.py",
                    "python": "/usr/bin/python3",
                    "script_sha256": "wrong" if conflict else source_sha,
                }
            )
        )

    monkeypatch.setattr(agent_transport.subprocess, "Popen", popen)
    monkeypatch.setattr(agent_transport.subprocess, "run", run)
    try:
        if conflict:
            with pytest.raises(ValueError, match="SHA mismatch"):
                agent_transport.main()
        else:
            assert agent_transport.main() == 0
            output = capsys.readouterr().out
            assert json.loads(output)["ready"] is True
            assert descriptor["token"] not in output
        assert launches == [
            [
                "ssh",
                "-o",
                "pinned",
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-R",
                "127.0.0.1:0:127.0.0.1:23456",
                "fixture@host",
            ]
        ]
        tunnel.terminate.assert_called_once()
        tunnel.wait.assert_called_once_with(timeout=5)
    finally:
        tunnel.stderr.close()
