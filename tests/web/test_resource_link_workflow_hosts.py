"""Offline host isolation; no SSH, controllers, cloud or user configuration."""

import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts.web.e2e.resource_link_workflow import native_continuation
from scripts.web.e2e.resource_link_workflow import worker_hosts as hosts
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.server import Service
from scripts.web.e2e.resource_link_workflow.store import Store
from tests.web.test_resource_link_workflow import plan, reserve


@pytest.fixture
def runtime(tmp_path):
    store = Store.initialize(tmp_path / "state")
    config = {
        "worker_host": "local",
        "bailian_endpoint": "ws://127.0.0.1:4501",
        "sol_endpoint": "ws://127.0.0.1:4502",
        "node": "fixture-node",
        "macmini": {
            "ip_resolver": "/local/bin/macmini-ip",
            "identity_file": str(tmp_path / "local/ssh/key"),
            "known_hosts": str(tmp_path / "local/ssh/known_hosts"),
            "user": "fixture",
            "python": "/usr/bin/python3",
            "chrome_executable": "/remote/apps/chrome",
            "chrome_profile": "/remote/test/chrome-profile",
            "worker_cwd": "/remote/test/workers",
        },
    }
    json_write(store.root / "environment.json", config, private=True)
    return Runtime(store)


def test_default_and_remote_binding_keep_sol_local(runtime):
    assert hosts.host({}) == "local"
    assert hosts.binding(runtime.config, "glm")["id"] == "bailian-local"
    runtime.config["worker_host"] = "macmini"
    remote = hosts.binding(runtime.config, "glm")
    assert remote == {
        "id": "alilang-macmini",
        "host": "macmini",
        "endpoint": "ws://127.0.0.1:4510",
        "cwd": "/remote/test/workers",
    }
    assert hosts.binding(runtime.config, "sol")["endpoint"] == "ws://127.0.0.1:4502"


@pytest.mark.parametrize("value", ["other", None, [], {}])
def test_invalid_host_no_fallback(value):
    with pytest.raises(WorkflowError):
        hosts.host({"worker_host": value})


@pytest.mark.parametrize(
    "endpoint",
    [None, "ws://elsewhere:4500", "ws://127.0.0.1:0", "ws://127.0.0.1:65536", "ws://127.0.0.1:4501/?secret=fixture"],
)
def test_invalid_backend_no_fallback(runtime, endpoint):
    runtime.config["bailian_endpoint"] = endpoint
    with pytest.raises(WorkflowError, match="configure"):
        hosts.binding(runtime.config, "glm")


@pytest.mark.parametrize("path", ["/", "/remote", "relative/profile", "/remote/../profile", "~/profile"])
def test_remote_profile_must_be_explicit(runtime, path):
    runtime.config["macmini"]["chrome_profile"] = path
    with pytest.raises(WorkflowError, match="paths"):
        hosts.mini(runtime.config)


def test_host_switch_restores_exact_targets_and_keeps_web(runtime):
    before = runtime.store.status()["slots"]
    with runtime.store.transaction() as con:
        for slot in before:
            con.execute("UPDATE slots SET target=? WHERE id=?", ("local-" + str(slot["id"]), slot["id"]))
    result = hosts.select_host(runtime, "macmini")
    assert result["web_and_sol_remain_local"]
    assert all(slot["target"] is None for slot in runtime.store.status()["slots"])
    with runtime.store.transaction() as con:
        con.execute("UPDATE slots SET target='remote-0' WHERE id=0")
    hosts.select_host(runtime, "local")
    restored = runtime.store.status()["slots"]
    assert [slot["target"] for slot in restored] == ["local-" + str(slot["id"]) for slot in before]
    assert [(slot["origin"], slot["cwd"]) for slot in restored] == [(slot["origin"], slot["cwd"]) for slot in before]
    hosts.select_host(runtime, "macmini")
    assert runtime.store.status()["slots"][0]["target"] == "remote-0"
    assert read_json(runtime.root / "environment.json")["worker_host"] == "macmini"


def test_switch_requires_pause_and_drained_jobs(runtime):
    runtime.store.pause(False)
    with pytest.raises(WorkflowError, match="pause and drain"):
        hosts.select_host(runtime, "macmini")
    runtime.store.pause(True)
    plan(runtime.store)
    reserve(runtime.store, "prepare")
    with pytest.raises(WorkflowError, match="pause and drain"):
        hosts.select_host(runtime, "macmini")


def test_torn_host_commit_fails_closed(runtime):
    with runtime.store.transaction() as con:
        con.execute("INSERT INTO meta VALUES('worker_host','macmini')")
    with pytest.raises(WorkflowError, match="commit mismatch"):
        runtime.attest_control()


@pytest.mark.parametrize("blocker", ["owned", "issued", "original-thread"])
def test_switch_rejects_unresolved_resources_actions_and_original_threads(runtime, blocker):
    plan(runtime.store)
    task_id = runtime.store.status()["tasks"][0]["id"]
    runtime.store.pause(True)
    with runtime.store.transaction() as con:
        if blocker == "owned":
            con.execute("INSERT INTO owned VALUES('fixture',?,'CREATE','{}','created','{}')", (task_id,))
        elif blocker == "issued":
            con.execute("INSERT INTO actions VALUES('fixture',?,'dispatch','issued',0)", (task_id,))
        else:
            con.execute(
                "INSERT INTO jobs VALUES('fixture',?,'glm','dispatch','complete','original',"
                "'exact-turn','fixture-capability','confirmed',0)",
                (task_id,),
            )
    with pytest.raises(WorkflowError):
        hosts.select_host(runtime, "macmini")
    assert read_json(runtime.root / "environment.json")["worker_host"] == "local"


@pytest.mark.parametrize("terminal", ["complete", "superseded"])
def test_switch_keeps_terminal_thread_history_without_blocking_the_next_host(runtime, terminal):
    plan(runtime.store)
    task_id = runtime.store.status()["tasks"][0]["id"]
    runtime.store.pause(True)
    with runtime.store.transaction() as con:
        con.execute("UPDATE tasks SET state=? WHERE id=?", (terminal, task_id))
        con.execute(
            "INSERT INTO jobs VALUES('history',?,'glm','dispatch','complete','original',"
            "'exact-turn','fixture-capability','confirmed',0)", (task_id,),
        )
    assert hosts.select_host(runtime, "macmini")["web_and_sol_remain_local"]
    assert read_json(runtime.root / "environment.json")["worker_host"] == "macmini"
    with runtime.store.transaction() as con:
        assert con.execute("SELECT thread_id FROM jobs WHERE id='history'").fetchone()[0] == "original"


def test_tunnel_uses_dynamic_pinned_ssh_and_only_loopback_forwards(runtime, monkeypatch):
    hosts.select_host(runtime, "macmini")
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="192.0.2.123\n"),
    )
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or Mock(pid=1234))
    monkeypatch.setattr(runtime, "require_free_port", lambda port: calls.append(port))
    monkeypatch.setattr(runtime, "process_identity", lambda pid: "fixture-owned-ssh")
    result = hosts.start_link(runtime)
    assert result["ready"] is False
    argv = spawned[0]
    assert argv[-1] == "fixture@192.0.2.123"
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert "127.0.0.1:4510:127.0.0.1:4500" in argv
    assert "127.0.0.1:9226:127.0.0.1:9225" in argv
    assert all(f"127.0.0.1:{port}:127.0.0.1:{port}" in argv for port in range(16100, 16104))
    assert calls[:3] == [4510, 9226, ["/local/bin/macmini-ip"]]
    assert (runtime.root / "macmini-link.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stdout", ["", "host.invalid", "192.0.2.1\n192.0.2.2", "192.0.2.1; bad"])
def test_invalid_discovered_ip_never_runs_ssh(runtime, monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=stdout))
    with pytest.raises(WorkflowError, match="discovery failed"):
        hosts.ssh_base(runtime.config)


def test_link_config_or_pid_conflict_is_not_restarted(runtime, monkeypatch):
    hosts.select_host(runtime, "macmini")
    json_write(runtime.root / "macmini-link.json", {"pid": 123, "identity": "original", "fingerprint": "wrong"})
    spawned = Mock()
    monkeypatch.setattr(subprocess, "Popen", spawned)
    with pytest.raises(WorkflowError, match="configuration changed"):
        hosts.start_link(runtime)
    spawned.assert_not_called()


def test_remote_browser_uses_forward_but_keeps_local_evidence(runtime, monkeypatch):
    hosts.select_host(runtime, "macmini")
    monkeypatch.setattr(hosts, "require_tunnel", lambda _: {})
    payloads = []

    def run(*args, input, **kw):
        payloads.append(json.loads(input))
        return subprocess.CompletedProcess([], 0, stdout='{"target":"remote-exact"}')

    monkeypatch.setattr(subprocess, "run", run)
    output = str(runtime.root / "artifacts/example")
    runtime.browser(
        {"operation": "evidence", "target": "remote-exact", "origin": "http://127.0.0.1:16100", "output_dir": output}
    )
    assert payloads[0]["cdp_endpoint"] == "http://127.0.0.1:9226"
    assert payloads[0]["remote_cdp"] is True and payloads[0]["output_dir"] == output
    assert payloads[0]["origin"] == "http://127.0.0.1:16100"


def test_remote_chrome_owner_verified_remotely_not_local_ps(runtime, monkeypatch):
    hosts.select_host(runtime, "macmini")
    monkeypatch.setattr(hosts, "require_tunnel", lambda _: {})
    seen = []
    monkeypatch.setattr(
        hosts,
        "remote_chrome",
        lambda config, request: (
            seen.append(request) or {"pid": 456, "identity": "remote-fixture", "profile": request["profile"]}
        ),
    )
    monkeypatch.setattr(runtime, "process_identity", Mock(side_effect=AssertionError("local ps is wrong")))
    assert runtime.start_chrome()["worker_host"] == "macmini"
    assert seen[0]["profile"] == "/remote/test/chrome-profile"
    assert not (runtime.root / "chrome.json").exists()


def test_launch_freezes_backend_in_database_and_marker_before_child(runtime, monkeypatch):
    plan(runtime.store)
    job = reserve(runtime.store, "prepare")
    service = Service(runtime.store)
    seen = []

    def spawn(*args, **kwargs):
        marker = read_json(runtime.root / "jobs" / job["job_id"] / "launch.json")
        with runtime.store.transaction() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", ("job_backend:" + job["job_id"],)).fetchone()
        assert marker["worker_backend"] == json.loads(row[0]) == hosts.binding(runtime.config, "sol")
        seen.append(marker)
        return Mock(pid=123)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(service.runtime, "process_identity", lambda pid: "owned-child")
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.server.threading.Thread", Mock())
    service.launch(job["job_id"])
    binding = service.request({"operation": "job-binding", "arguments": {"job_id": job["job_id"]}})
    assert binding["worker_backend"] == seen[0]["worker_backend"]
    saved = read_json(runtime.root / "jobs" / job["job_id"] / "launch.json")
    assert saved["worker_backend"] == binding["worker_backend"]


def test_mini_job_read_requires_original_owned_link_even_after_switch_local(runtime, monkeypatch):
    plan(runtime.store)
    job = reserve(runtime.store, "prepare")
    owner = {
        "id": "alilang-macmini",
        "host": "macmini",
        "endpoint": "ws://127.0.0.1:4510",
        "cwd": "/remote/test/workers/job",
        "link_fingerprint": "a" * 64,
    }
    with runtime.store.transaction() as con:
        con.execute("UPDATE jobs SET role='glm' WHERE id=?", (job["job_id"],))
        con.execute("INSERT INTO meta VALUES(?,?)", ("job_backend:" + job["job_id"], json.dumps(owner)))
    service = Service(runtime.store)
    monkeypatch.setattr(hosts, "require_tunnel", lambda _: {"fingerprint": "b" * 64})
    payload = {"operation": "job-binding", "arguments": {"job_id": job["job_id"]}}
    with pytest.raises(WorkflowError, match="original owned"):
        service.request(payload)
    monkeypatch.setattr(hosts, "require_tunnel", lambda _: {"fingerprint": "a" * 64})
    assert service.request(payload)["worker_backend"] == owner


def test_remote_launch_reuses_transaction_without_recursive_status(runtime, monkeypatch):
    hosts.select_host(runtime, "macmini")
    plan(runtime.store)
    job = reserve(runtime.store, "prepare")
    with runtime.store.transaction() as con:
        con.execute("UPDATE jobs SET role='glm' WHERE id=?", (job["job_id"],))
    service = Service(runtime.store)
    ports = list(range(16100, 16104))
    json_write(
        runtime.root / "macmini-link.json",
        {
            "pid": 123,
            "identity": "owned-link",
            "fingerprint": hosts.tunnel_fingerprint(runtime.config, ports),
        },
    )
    monkeypatch.setattr(runtime.store, "status", Mock(side_effect=AssertionError("nested write transaction")))
    monkeypatch.setattr(service.runtime, "assert_process", lambda *args: None)
    monkeypatch.setattr(hosts, "remote_chrome", lambda *args: {"workspace_ready": True})
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Mock(pid=456))
    monkeypatch.setattr(service.runtime, "process_identity", lambda pid: "adapter-child")
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.server.threading.Thread", Mock())
    assert service.launch(job["job_id"])["launched"]
    saved = read_json(runtime.root / "jobs" / job["job_id"] / "launch.json")
    assert saved["worker_backend"]["id"] == "alilang-macmini"
    with pytest.raises(WorkflowError, match="existing launch marker"):
        service.launch(job["job_id"])


def _reviewed_continuation(runtime, monkeypatch):
    plan(runtime.store)
    job = reserve(runtime.store, "prepare")
    task_id = runtime.store.status()["tasks"][0]["id"]
    original_thread = "original-glm-thread"
    with runtime.store.transaction() as con:
        con.execute("UPDATE tasks SET state='running',attempt=3,slot=0 WHERE id=?", (task_id,))
        con.execute("UPDATE slots SET task_id=?,target='original-target' WHERE id=0", (task_id,))
        con.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "original",
                task_id,
                "glm",
                "dispatch",
                "complete",
                original_thread,
                "original-turn",
                "fixture-capability",
                "confirmed",
                0,
            ),
        )
        con.execute(
            "UPDATE jobs SET role='glm',purpose='continuation',thread_id=?,turn_id=NULL WHERE id=?",
            (original_thread, job["job_id"]),
        )
        slot = con.execute("SELECT * FROM slots WHERE id=0").fetchone()
    proof = {
        "approved": True,
        "task_id": task_id,
        "original_job_id": "original",
        "attempt": 2,
        "thread_id": original_thread,
        "original_turn_id": "original-turn",
        "slot": 0,
        "target": "original-target",
        "origin": slot["origin"],
        "scope": "original:QUERY",
        "session": "original-session",
    }
    proof_path = runtime.root / "inputs/review.json"
    json_write(proof_path, proof, private=True)
    recovery = {
        "original_job_id": "original",
        "evidence": "inputs/review.json",
        "evidence_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        "scope": proof["scope"],
        "session_id": proof["session"],
    }
    monkeypatch.setattr(native_continuation, "load", lambda *args: recovery)
    runtime.store.set_review_only("Preserve original generation under OPS review")
    return job, proof


def test_review_only_launch_allows_only_exact_registered_continuation(runtime, monkeypatch):
    job, _ = _reviewed_continuation(runtime, monkeypatch)
    service = Service(runtime.store)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Mock(pid=456))
    monkeypatch.setattr(service.runtime, "process_identity", lambda pid: "adapter-child")
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.server.threading.Thread", Mock())
    assert service.launch(job["job_id"])["launched"]


def test_review_only_launch_rejects_continuation_binding_drift(runtime, monkeypatch):
    job, _ = _reviewed_continuation(runtime, monkeypatch)
    with runtime.store.transaction() as con:
        con.execute("UPDATE jobs SET thread_id='other-thread' WHERE id=?", (job["job_id"],))
    service = Service(runtime.store)
    with pytest.raises(WorkflowError, match="exact OPS review"):
        service.launch(job["job_id"])


def test_review_only_launch_still_blocks_normal_jobs(runtime):
    plan(runtime.store)
    job = reserve(runtime.store, "prepare")
    runtime.store.set_review_only("Preserve original generation under OPS review")
    with pytest.raises(WorkflowError, match="review-only hold"):
        Service(runtime.store).launch(job["job_id"])


def test_review_only_submission_boundary_allows_only_exact_continuation(runtime, monkeypatch):
    job, _ = _reviewed_continuation(runtime, monkeypatch)
    capability = read_json(runtime.root / "jobs" / job["job_id"] / "capability.json")
    runtime.store.mark_submission(job["job_id"], capability["token"])
    with runtime.store.transaction() as con:
        row = con.execute("SELECT submission FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
    assert row["submission"] == "may_have_submitted"


def test_remote_ops_script_is_valid_python_without_remote_execution():
    compile(hosts.REMOTE_CHROME, "fixture-remote-helper", "exec")
