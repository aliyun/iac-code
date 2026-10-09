"""A reachable foreign agent card must not make an unbound child ready."""

import io
import itertools
import sys
from types import SimpleNamespace

import pytest

from scripts.a2a.e2e import common
from scripts.a2a.e2e import run_recovery_scenarios as recovery
from scripts.a2a.e2e.resource_selector import live_pipeline_server


@pytest.mark.parametrize("return_code", [1, None])
def test_recovery_start_rejects_foreign_endpoint_when_own_child_has_not_bound(tmp_path, monkeypatch, return_code):
    calls = []

    def foreign_card(*args, **kwargs):
        calls.append(True)
        response = io.BytesIO(b"{}")
        response.status = 200
        return response

    def start(server):
        server.process = SimpleNamespace(poll=lambda: return_code)

    ticks = itertools.count(step=0.1)
    monkeypatch.setattr(common.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(common.time, "sleep", lambda _: None)
    monkeypatch.setattr(common, "urlopen", foreign_card)
    monkeypatch.setattr(common.ManagedServer, "start", start)
    monkeypatch.setattr(recovery, "ManagedServer", common.ManagedServer)
    monkeypatch.setattr(recovery, "wait_for_server", common.wait_for_server)
    harness = recovery.ScenarioHarness.__new__(recovery.ScenarioHarness)
    harness.server_index = 0
    harness.args = SimpleNamespace(python="python", server_timeout=0.5)
    harness.config_path = tmp_path / "fixture.yml"
    harness.server_cwd = str(tmp_path)
    harness.cwd = str(tmp_path)
    harness.server_env = {}
    harness.run_dir = tmp_path
    harness.server_url = "http://127.0.0.1:12345"

    with pytest.raises(RuntimeError):
        harness.start_server()
    assert not calls, "contacted another endpoint before own server acquired its port"


@pytest.mark.parametrize(
    ("bound_url", "ready"),
    [
        ("http://127.0.0.1:12345", True),
        ("http://127.0.0.1:12346", False),
    ],
)
def test_owned_endpoint_requires_child_bind_receipt_and_live_process(tmp_path, monkeypatch, bound_url, ready):
    calls = []

    def card(*args, **kwargs):
        calls.append(True)
        response = io.BytesIO(b"{}")
        response.status = 200
        return response

    monkeypatch.setattr(common, "urlopen", card)
    server = common.ManagedServer(
        python_cmd=["python"],
        config_path=tmp_path / "fixture.yml",
        process_cwd=str(tmp_path),
        allowed_cwd=str(tmp_path),
        env={},
        log_prefix=tmp_path / "server",
    )
    server.process = SimpleNamespace(poll=lambda: None)
    server._record_startup_line("INFO: Application startup complete.\n")
    assert server._listening_url is None
    server._record_startup_line(
        "\x1b[32mINFO:\x1b[0m Uvicorn running on \x1b[1m" + bound_url + "\x1b[0m (Press CTRL+C to quit)\n"
    )
    if ready:
        common.wait_for_server("http://127.0.0.1:12345", timeout=0.5, owned_server=server)
        assert calls == [True]
    else:
        with pytest.raises(RuntimeError, match="different endpoint"):
            common.wait_for_server("http://127.0.0.1:12345", timeout=0.5, owned_server=server)
        assert not calls


def test_selector_pipeline_server_keeps_owned_bind_receipt_visible(tmp_path, monkeypatch):
    import uvicorn

    from iac_code import config
    from iac_code.a2a import app, pipeline_executor

    root = tmp_path / 'pipelines'
    pipeline = root / 'fixture'
    pipeline.mkdir(parents=True)
    (pipeline / 'pipeline.yaml').write_text('name: fixture\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['server', '--port', '12345', '--config-dir', str(tmp_path / 'config'),
        '--persistence-dir', str(tmp_path / 'persistence'), '--artifact-dir', str(tmp_path / 'artifacts'),
        '--workspace', str(tmp_path / 'workspace'), '--pipeline-root', str(root), '--pipeline-name', 'fixture'])
    monkeypatch.setattr(app, 'create_app', lambda **kwargs: object())
    monkeypatch.setattr(config, 'load_saved_model', lambda: 'fake-model')
    for name in ('IAC_CODE_CONFIG_DIR', 'IAC_CODE_MODE', 'IAC_CODE_PIPELINE_NAME', 'IACCODE_A2A_ALLOWED_CWDS'):
        monkeypatch.setenv(name, '')
    # main replaces these production functions; restore them after this test.
    monkeypatch.setattr(pipeline_executor, 'discover_pipelines', pipeline_executor.discover_pipelines)
    monkeypatch.setattr(pipeline_executor, 'create_pipeline', pipeline_executor.create_pipeline)
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: calls.append(kwargs))
    assert live_pipeline_server.main() == 0
    assert calls[0]['log_level'] == 'info', 'warning suppresses the owned socket-bind receipt'
