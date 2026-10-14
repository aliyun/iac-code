"""Read-only original-session click recovery; no real browser, account or network."""

import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.execution_gate import database, initialize
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps


@pytest.fixture
def navigation(tmp_path):
    store = Store.initialize(tmp_path / "state")
    root = store.root
    job, task, scene = "a" * 32, "offline:R0020", "R0020-ROS-1-open"
    source_type, session, target = "ALIYUN::ECS::SNatEntry", "ws~offline-full-key", "offline-target"
    dimensions = {dim: {"support": "not_applicable", "contracts": []} for dim in ("ROS", "TF", "CREATE", "QUERY")}
    dimensions["ROS"] = {
        "support": "supported",
        "creation_verified": False,
        "contracts": [
            {
                "id": "R0020-ROS-1",
                "source_type": source_type,
                "historical": {"iframe": "pass", "open": "fail"},
            }
        ],
    }
    spec = {
        "id": "R0020",
        "key": source_type,
        "locks": ["original-resource"],
        "dimensions": dimensions,
        "retest_scope": {"kind": "failed_navigation_existing", "scene_ids": [scene]},
    }
    output = root / "jobs" / job / "output"
    bound = {"job_id": job, "session_id": session, "target": target, "origin": "http://127.0.0.1:16100"}
    json_write(output / "self-created-session.json", bound)
    json_write(output / "new-session-intent.json", {"previous_session": "previous-full-key"})
    reference = {
        "identifier": "snat-offline123",
        "resourceType": source_type,
        "regionId": "cn-hangzhou",
        "accountScope": "opaque-offline",
        "sources": [
            {
                "toolUseId": "call_original_ros",
                "turnId": "original-turn",
                "evidence": "api_response",
                "product": "ros",
                "action": "GetStackResource",
            }
        ],
    }
    json_write(output / "ROS-product-source.json", {"session_id": session, "references": {"items": [reference]}})
    json_write(output / f"{scene}.json", {"session_id": session, "actual_ui_click": False})
    json_write(root / "inputs/seed.json", {"offline_fixture": True})
    packet = {
        "task_id": task,
        "budget_cents": 0,
        "inputs": [],
        "ui": {},
        "requests": {"ROS": "readonly"},
        "scenes": {
            scene: {
                "account": "fake-account",
                "region": "cn-hangzhou",
                "identifier": reference["identifier"],
                "expected_type": source_type,
                "selector": 'a.resource-link[aria-label="snat-offline123"]',
                "render_text": ["offline-name"],
                "source_evidence": "inputs/seed.json",
                "source_sha256": digest(root / "inputs/seed.json"),
            }
        },
    }
    json_write(root / "packets/original.json", packet)
    with store.transaction() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?)", ("offline", dumps({"resources": [spec]}), "active", 0))
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)",
            (task, "offline", "R0020", dumps(spec), "blocked", 2, None, "packets/original.json", "gap"),
        )
        db.execute("UPDATE slots SET target=? WHERE id=0", (target,))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job, task, "glm", "dispatch", "complete", "same-persistent-thread", "original-turn", "", "", 1),
        )
        db.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (task, scene, "product_gap", "{}", 2),
        )
    initialize(root / "execution-gate.sqlite3")
    with database(root / "execution-gate.sqlite3") as db:
        call = dumps({"product": "ros", "action": "GetStackResource", "region_id": "cn-hangzhou", "params": {}})
        db.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used) VALUES(?,?,?,?,?,?)",
            (job + ":ROS", "read", "read", call, 1, 1),
        )
        db.execute(
            "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created,"
            "reference_scope,tool_use_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "attempt1",
                job + ":ROS",
                "read",
                session,
                "succeeded",
                "{}",
                "fake-signer",
                1,
                "opaque-offline",
                "call_original_ros",
            ),
        )
        db.execute("INSERT INTO identities VALUES(?,?,?,?)", (job + ":ROS", session, "fake-signer", "fake-account"))
        wire = {
            "grants": [dict(row) for row in db.execute("SELECT * FROM grants ORDER BY scope,id")],
            "attempts": [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")],
        }
    proof = {
        "schema_version": 1,
        "kind": "ops_navigation_only_review",
        "approved": True,
        "task_id": task,
        "original_job_id": job,
        "scene_ids": [scene],
        "cloud_calls": 0,
        "session_id": session,
        "target": target,
        "origin": bound["origin"],
        "packet_sha256": digest(root / "packets/original.json"),
        "session_binding_sha256": digest(output / "self-created-session.json"),
        "sources": {
            "ROS": {
                "path": str((output / "ROS-product-source.json").relative_to(root)),
                "sha256": digest(output / "ROS-product-source.json"),
            }
        },
        "wire_sha256": hashlib.sha256(dumps(wire).encode()).hexdigest(),
    }
    json_write(root / "inputs/review.json", proof)
    return store, job, scene, proof


def start(store, original):
    store.queue_navigation(original, "inputs/review.json")
    store.pause(False)
    action = store.next_action()["actions"][0]
    assert action["kind"] == "navigation"
    job = store.execute(action["id"])["job_id"]
    token = read_json(store.root / "jobs" / job / "capability.json")["token"]
    store.bind(job, token, "same-persistent-thread", "new-click-turn")
    return job, token


def test_recovery_is_luna_only_same_thread_slot_new_output_no_wire_changes(navigation, monkeypatch):
    store, original, scene, proof = navigation
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    job, token = start(store, original)
    context = store.context(job, token)
    assert context["attempt"] == 3 and context["slot"]["id"] == 0
    assert context["navigation_recovery"]["original_job_id"] == original
    assert not (store.root / "jobs" / job / "output/new-session-intent.json").exists()
    runtime = Runtime(store)

    def browser(request):
        assert request["operation"] == "click"
        assert request["session_id"] == proof["session_id"]
        assert request["selector"].startswith('details[data-tool-use-id="call_original_ros"] ')
        screenshot = store.root / "jobs" / job / "output" / f"{scene}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"offline-not-a-real-acceptance-image")
        return {"session_id": proof["session_id"], "actual_ui_click": True, "screenshot": str(screenshot)}

    monkeypatch.setattr(runtime, "browser", browser)
    result = runtime.tool(job, token, "click", {"scene": scene})
    assert result["source_evidence"] == proof["sources"]["ROS"]["path"]
    assert result["source_sha256"] == proof["sources"]["ROS"]["sha256"]
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before
    assert read_json(store.root / "jobs" / original / "output" / f"{scene}.json")["actual_ui_click"] is False
    with pytest.raises(WorkflowError, match="replay an existing scene"):
        runtime.tool(job, token, "click", {"scene": scene})


@pytest.mark.parametrize(
    "operation",
    [
        "new-session",
        "request",
        "source",
        "continue-read",
        "cleanup",
        "cleanup-source",
    ],
)
def test_recovery_cannot_create_read_send_or_cleanup(navigation, monkeypatch, operation):
    store, original, _, _ = navigation
    job, token = start(store, original)
    runtime = Runtime(store)
    monkeypatch.setattr(runtime, "browser", lambda _: pytest.fail("browser must not run"))
    with pytest.raises(WorkflowError, match="navigation-only recovery forbids"):
        runtime.tool(job, token, operation, {"dimension": "ROS"})


@pytest.mark.parametrize(
    "damage",
    [
        "not_paused",
        "active_job",
        "issued_action",
        "slot_taken",
        "other_session",
        "source_drift",
        "wire_drift",
        "unknown",
        "write",
        "clicked",
        "pass",
        "unapproved",
        "wrong_task",
        "wrong_scene",
        "new_job",
        "packet_drift",
        "session_drift",
        "cleanup_started",
        "wrong_account",
        "wrong_scope",
        "wrong_type",
    ],
)
def test_recovery_rejects_unreviewed_or_unproven_replay(navigation, damage):
    store, job, scene, proof = navigation
    output = store.root / "jobs" / job / "output"
    with store.transaction() as db:
        if damage == "not_paused":
            db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif damage == "active_job":
            db.execute("UPDATE jobs SET state='unknown'")
        elif damage == "issued_action":
            db.execute("INSERT INTO actions VALUES('issued','offline:R0020','prepare','issued',0)")
        elif damage == "slot_taken":
            db.execute("UPDATE slots SET task_id='other' WHERE id=0")
        elif damage == "pass":
            db.execute("UPDATE verdicts SET status='pass'")
    if damage in {"other_session", "wrong_task", "wrong_scene", "unapproved", "new_job", "wrong_scope"}:
        key, value = {
            "other_session": ("session_id", "wrong"),
            "wrong_task": ("task_id", "other"),
            "wrong_scene": ("scene_ids", ["R0020-ROS-1-iframe"]),
            "unapproved": ("approved", False),
            "new_job": ("original_job_id", "missing"),
            "wrong_scope": ("wire_sha256", "wrong"),
        }[damage]
        proof[key] = value
    elif damage in {"source_drift", "wrong_type"}:
        value = read_json(output / "ROS-product-source.json")
        value["references"]["items"][0]["resourceType"] = "ALIYUN::VPC::SnatEntry"
        json_write(output / "ROS-product-source.json", value)
        if damage == "wrong_type":
            proof["sources"]["ROS"]["sha256"] = digest(output / "ROS-product-source.json")
    elif damage == "clicked":
        json_write(output / f"{scene}.json", {"session_id": proof["session_id"], "actual_ui_click": True})
    elif damage == "packet_drift":
        json_write(store.root / "packets/original.json", {})
    elif damage == "session_drift":
        json_write(output / "self-created-session.json", {})
    elif damage == "cleanup_started":
        json_write(output / "ROS-cleanup-intent.json", {})
    elif damage in {"wire_drift", "unknown", "write", "wrong_account"}:
        with database(store.root / "execution-gate.sqlite3") as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown'")
            elif damage == "write":
                db.execute("UPDATE grants SET kind='create'")
            elif damage == "wrong_account":
                db.execute("UPDATE identities SET account='other'")
            else:
                db.execute("UPDATE grants SET used=2")
    json_write(store.root / "inputs/review.json", proof)
    with pytest.raises((WorkflowError, KeyError)):
        store.queue_navigation(job, "inputs/review.json")
    assert not any(item["purpose"] == "navigation" for item in store.status()["jobs"])


def test_recovery_rechecks_original_artifacts_on_every_operation(navigation):
    store, original, _, proof = navigation
    job, token = start(store, original)
    json_write(store.root / proof["sources"]["ROS"]["path"], {})
    with pytest.raises(WorkflowError, match="source/session changed"):
        Runtime(store).tool(job, token, "snapshot", {})


@pytest.mark.parametrize("damage", [None, "success", "unknown", "clicked", "raw_drift", "wrong_turn", "reused"])
def test_navigation_retry_requires_fresh_terminal_unclicked_proof(navigation, damage):
    store, original, scene, proof = navigation
    job, _ = start(store, original)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='complete' WHERE id=?", (job,))
        db.execute("UPDATE tasks SET state='blocked',slot=NULL")
        db.execute("UPDATE slots SET task_id=NULL")
        db.execute("DELETE FROM locks")
        if damage == "reused":
            db.execute("INSERT INTO meta VALUES(?,?)", ("navigation_retry:" + job, "{}"))
    store.pause(True)
    json_write(
        store.root / "jobs" / job / "output" / f"{scene}.json",
        {
            "actual_ui_click": damage == "clicked",
            "session_id": proof["session_id"],
        },
    )
    raw = {
        "thread": {
            "id": "same-persistent-thread",
            "turns": [
                {
                    "id": "wrong" if damage == "wrong_turn" else "new-click-turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "dynamicToolCall",
                            "tool": "resource_workflow",
                            "arguments": {"operation": "click"},
                            "success": {"success": True, "unknown": None}.get(damage, False),
                        }
                    ],
                }
            ],
        }
    }
    json_write(store.root / "inputs/terminal.json", raw)
    proof.update(
        retry_job_id=job,
        retry_terminal_evidence="inputs/terminal.json",
        retry_terminal_sha256=digest(store.root / "inputs/terminal.json"),
    )
    json_write(store.root / "inputs/retry.json", proof)
    if damage == "raw_drift":
        json_write(store.root / "inputs/terminal.json", {})
    if damage:
        with pytest.raises(WorkflowError):
            store.queue_navigation(original, "inputs/retry.json")
    else:
        result = store.queue_navigation(original, "inputs/retry.json")
        assert result["cloud_calls"] == 0
        with store.transaction() as db:
            archived = db.execute("SELECT value FROM meta WHERE key=?", ("navigation_retry:" + job,)).fetchone()
            assert archived and "inputs/review.json" in archived[0]


def sae_capture_fixture(navigation):
    from test_resource_link_workflow_sae_application import specimen

    store, original, old_scene, proof = navigation
    root = store.root
    output = root / "jobs" / original / "output"
    _, inspection, reference, tool, _ = specimen()
    scene = "R0020-QUERY-1-open"
    reference.update(accountScope="opaque-offline")
    reference["sources"][0].update(toolUseId="call_original_ros", turnId="original-turn")
    tool["results"][0]["toolUseId"] = "call_original_ros"
    source = {
        "session_id": proof["session_id"],
        "references": {"items": [reference]},
        "messages": {"tools": {"call_original_ros": tool}},
    }
    json_write(output / "QUERY-product-source.json", source)
    source_path = str((output / "QUERY-product-source.json").relative_to(root))
    source_sha = digest(output / "QUERY-product-source.json")
    packet = read_json(root / "packets/original.json")
    packet["requests"] = {"QUERY": "read only"}
    binding = packet["scenes"].pop(old_scene)
    binding.update(
        identifier=inspection["identifier"],
        expected_type=inspection["resource_type"],
        account=inspection["account"],
        region=inspection["region"],
        render_text=["testdesc", "desc"],
        selector=f'a.resource-link[aria-label="{inspection["identifier"]}"]',
    )
    packet["scenes"][scene] = binding
    json_write(root / "packets/original.json", packet)
    png = output / f"{scene}.png"
    png.write_bytes(b"offline unchanged original pixels")
    click = {
        "session_id": proof["session_id"],
        "actual_ui_click": True,
        "source_evidence": source_path,
        "source_sha256": source_sha,
        "screenshot": str(png.relative_to(root)),
        "clicked_occurrence": {"index": 1, "focus": "offline-focus", "href": "https://example.invalid/own"},
    }
    json_write(output / f"{scene}.json", click)
    diagnosis = {
        "kind": "sol_sae_viewport_sampler_repair_handoff",
        "job_id": "repair-offline",
        "task_id": proof["task_id"],
        "source_evidence": {"path": source_path, "sha256": source_sha},
    }
    json_write(root / "inputs/R0020/diagnosis.json", diagnosis)
    with store.transaction() as db:
        task = db.execute("SELECT spec FROM tasks").fetchone()
        spec = json.loads(task[0])
        contract = spec["dimensions"]["ROS"]["contracts"][0]
        contract.update(id="R0020-QUERY-1", source_type=inspection["resource_type"])
        spec["dimensions"]["QUERY"] = spec["dimensions"]["ROS"]
        spec["dimensions"]["ROS"] = {"support": "unknown", "contracts": []}
        spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": [scene]}
        db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        db.execute("UPDATE verdicts SET scene=?,status='fail'", (scene,))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("repair-offline", proof["task_id"], "sol", "repair", "complete", "own-sol", "repair-turn", "", "", 2),
        )
    with database(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET scope=?,call=?", (original + ":QUERY", dumps(tool["input"])))
        db.execute("UPDATE attempts SET scope=?", (original + ":QUERY",))
        db.execute("UPDATE identities SET scope=?,account=?", (original + ":QUERY", inspection["account"]))
        wire = {
            "grants": [dict(row) for row in db.execute("SELECT * FROM grants ORDER BY scope,id")],
            "attempts": [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")],
        }
    proof.update(
        scene_ids=[scene],
        packet_sha256=digest(root / "packets/original.json"),
        sources={"QUERY": {"path": source_path, "sha256": source_sha}},
        wire_sha256=hashlib.sha256(dumps(wire).encode()).hexdigest(),
        capture_recovery={
            "kind": "sae_application_field_capture",
            "console_inspection": "sae_application_metadata",
            "cloud_calls": 0,
            "remediation_evidence": "inputs/R0020/diagnosis.json",
            "remediation_sha256": digest(root / "inputs/R0020/diagnosis.json"),
            "original_clicks": {
                scene: {
                    "path": str((output / f"{scene}.json").relative_to(root)),
                    "sha256": digest(output / f"{scene}.json"),
                    "screenshot": click["screenshot"],
                    "screenshot_sha256": digest(png),
                }
            },
        },
    )
    json_write(root / "inputs/review.json", proof)
    return store, original, scene, proof


def test_sae_failed_capture_recovery_keeps_source_wire_and_occurrence(navigation, monkeypatch):
    store, original, scene, proof = sae_capture_fixture(navigation)
    wire_before = (store.root / "execution-gate.sqlite3").read_bytes()
    original_before = (store.root / "jobs" / original / "output" / f"{scene}.png").read_bytes()
    job, token = start(store, original)
    runtime = Runtime(store)

    def browser(request):
        assert request["console_inspection"]["kind"] == "sae_application_metadata"
        assert request["recovery_occurrence"]["index"] == 1
        assert request["session_id"] == proof["session_id"]
        png = store.root / "jobs" / job / "output" / f"{scene}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"offline new original viewport")
        return {"session_id": proof["session_id"], "actual_ui_click": True, "screenshot": str(png)}

    monkeypatch.setattr(runtime, "browser", browser)
    assert runtime.tool(job, token, "click", {"scene": scene})["navigation_recovery"]["cloud_calls"] == 0
    assert (store.root / "execution-gate.sqlite3").read_bytes() == wire_before
    assert (store.root / "jobs" / original / "output" / f"{scene}.png").read_bytes() == original_before


@pytest.mark.parametrize(
    "damage",
    [
        "pass",
        "other-type",
        "other-inspector",
        "source-drift",
        "png-drift",
        "diagnosis-drift",
        "active-repair",
        "new-cloud-call",
        "missing-click",
        "wire-drift",
    ],
)
def test_sae_capture_recovery_rejects_unreviewed_replays(navigation, damage):
    store, original, scene, proof = sae_capture_fixture(navigation)
    root = store.root
    with store.transaction() as db:
        if damage == "pass":
            db.execute("UPDATE verdicts SET status='pass'")
        elif damage == "active-repair":
            db.execute("UPDATE jobs SET state='running' WHERE id='repair-offline'")
        elif damage == "other-type":
            spec = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
            spec["dimensions"]["QUERY"]["contracts"][0]["source_type"] = "ALIYUN::SAE::Namespace"
            db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
    if damage == "other-inspector":
        proof["capture_recovery"]["console_inspection"] = "oos_template_metadata"
    elif damage == "new-cloud-call":
        proof["capture_recovery"]["cloud_calls"] = 1
    elif damage == "source-drift":
        json_write(root / proof["sources"]["QUERY"]["path"], {})
    elif damage == "png-drift":
        (root / "jobs" / original / "output" / f"{scene}.png").write_bytes(b"changed")
    elif damage == "diagnosis-drift":
        json_write(root / "inputs/R0020/diagnosis.json", {})
    elif damage == "missing-click":
        json_write(root / "jobs" / original / "output" / f"{scene}.json", {"actual_ui_click": False})
    elif damage == "wire-drift":
        with database(root / "execution-gate.sqlite3") as db:
            db.execute("UPDATE grants SET used=2")
    json_write(root / "inputs/review.json", proof)
    with pytest.raises((WorkflowError, KeyError)):
        store.queue_navigation(original, "inputs/review.json")


def oos_capture_fixture(navigation):
    from test_resource_link_workflow_oos_state_configuration import fixture

    store, original, old_scene, proof = navigation
    root, output = store.root, store.root / "jobs" / original / "output"
    binding, inspection, reference, tool, _ = fixture()
    scenes = ["R0020-QUERY-1-iframe", "R0020-QUERY-1-open"]
    reference.update(accountScope="opaque-offline")
    reference["sources"][0].update(toolUseId="call_original_ros", turnId="original-turn")
    tool["results"][0]["toolUseId"] = "call_original_ros"
    source = {
        "session_id": proof["session_id"],
        "references": {"items": [reference]},
        "messages": {"tools": {"call_original_ros": tool}},
    }
    source_path = str((output / "QUERY-product-source.json").relative_to(root))
    json_write(root / source_path, source)
    source_sha = digest(root / source_path)
    binding.update(
        identifier=inspection["identifier"],
        expected_type=inspection["resource_type"],
        selector=f'a.resource-link[aria-label="{inspection["identifier"]}"]',
        source_evidence="inputs/seed.json",
        source_sha256=digest(root / "inputs/seed.json"),
    )
    packet = read_json(root / "packets/original.json")
    packet.update(requests={"QUERY": "readonly"}, scenes={scene: dict(binding) for scene in scenes})
    json_write(root / "packets/original.json", packet)
    original_clicks = {}
    for scene in scenes:
        png, artifact = output / f"{scene}.png", output / f"{scene}.json"
        png.write_bytes(b"offline original OOS missing native ID")
        click = {
            "session_id": proof["session_id"],
            "actual_ui_click": True,
            "source_evidence": source_path,
            "source_sha256": source_sha,
            "screenshot": str(png.relative_to(root)),
            "clicked_occurrence": {"index": 0, "focus": "own-occurrence", "href": "https://example.invalid/own"},
        }
        json_write(artifact, click)
        original_clicks[scene] = {
            "path": str(artifact.relative_to(root)),
            "sha256": digest(artifact),
            "screenshot": click["screenshot"],
            "screenshot_sha256": digest(png),
        }
    diagnosis = {
        "kind": "sol_oos_input_repair_capture_handoff",
        "task_id": proof["task_id"],
        "job_id": "repair-offline",
        "source_evidence": {"path": source_path, "sha256": source_sha},
    }
    json_write(root / "inputs/R0020/diagnosis.json", diagnosis)
    with store.transaction() as db:
        spec = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
        spec["dimensions"]["QUERY"] = {
            "support": "supported",
            "creation_verified": False,
            "contracts": [
                {
                    "id": "R0020-QUERY-1",
                    "source_type": inspection["resource_type"],
                    "historical": {"iframe": "fail", "open": "fail"},
                }
            ],
        }
        spec["dimensions"]["ROS"] = {"support": "unknown", "contracts": []}
        spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": scenes}
        db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        db.execute("DELETE FROM verdicts WHERE scene=?", (old_scene,))
        for scene in scenes:
            db.execute(
                "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
                (proof["task_id"], scene, "execution_gap", "{}", 2),
            )
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("repair-offline", proof["task_id"], "sol", "repair", "complete", "own-sol", "repair-turn", "", "", 2),
        )
    with database(root / "execution-gate.sqlite3") as db:
        db.execute("UPDATE grants SET scope=?,call=?", (original + ":QUERY", dumps(tool["input"])))
        db.execute("UPDATE attempts SET scope=?", (original + ":QUERY",))
        db.execute("UPDATE identities SET scope=?,account=?", (original + ":QUERY", inspection["account"]))
        wire = {
            "grants": [dict(row) for row in db.execute("SELECT * FROM grants ORDER BY scope,id")],
            "attempts": [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")],
        }
    proof.update(
        scene_ids=scenes,
        packet_sha256=digest(root / "packets/original.json"),
        sources={"QUERY": {"path": source_path, "sha256": source_sha}},
        wire_sha256=hashlib.sha256(dumps(wire).encode()).hexdigest(),
        capture_recovery={
            "kind": "oos_state_configuration_field_capture",
            "console_inspection": "oos_state_configuration_list",
            "cloud_calls": 0,
            "remediation_evidence": "inputs/R0020/diagnosis.json",
            "remediation_sha256": digest(root / "inputs/R0020/diagnosis.json"),
            "original_clicks": original_clicks,
        },
    )
    json_write(root / "inputs/review.json", proof)
    return store, original, scenes, proof


def test_oos_capture_recovery_two_modes_uses_own_inspector_and_immutable_source(navigation, monkeypatch):
    store, original, scenes, proof = oos_capture_fixture(navigation)
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    original_bytes = {
        scene: (store.root / proof["capture_recovery"]["original_clicks"][scene]["path"]).read_bytes()
        for scene in scenes
    }
    job, token = start(store, original)
    runtime = Runtime(store)

    def browser(request):
        assert request["console_inspection"]["kind"] == "oos_state_configuration_list"
        assert request["console_inspection"]["identifier"] == "sc-offline"
        assert request["recovery_occurrence"]["focus"] == "own-occurrence"
        assert request["session_id"] == proof["session_id"]
        png = store.root / "jobs" / job / "output" / f"{request['artifact_id']}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"offline new pixels; not an acceptance")
        return {"session_id": proof["session_id"], "actual_ui_click": True, "screenshot": str(png)}

    monkeypatch.setattr(runtime, "browser", browser)
    for scene in scenes:
        result = runtime.tool(job, token, "click", {"scene": scene})
        assert result["source_evidence"] == proof["sources"]["QUERY"]["path"]
        original_path = store.root / proof["capture_recovery"]["original_clicks"][scene]["path"]
        assert original_path.read_bytes() == original_bytes[scene]
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "pass",
        "fail",
        "other-type",
        "other-inspector",
        "source-drift",
        "png-drift",
        "diagnosis-drift",
        "active-repair",
        "new-cloud-call",
        "missing-click",
        "wire-drift",
        "metadata-drift",
        "different-occurrence",
        "no-native-id",
        "unknown-wire",
        "write-wire",
        "wrong-diagnosis",
        "duplicate",
    ],
)
def test_oos_capture_recovery_rejects_unsafe_or_changed_provenance(navigation, damage):
    store, original, scenes, proof = oos_capture_fixture(navigation)
    root, scene = store.root, scenes[0]
    with store.transaction() as db:
        if damage in {"pass", "fail"}:
            db.execute("UPDATE verdicts SET status=? WHERE scene=?", (damage, scene))
        elif damage == "active-repair":
            db.execute("UPDATE jobs SET state='running' WHERE id='repair-offline'")
        elif damage == "other-type":
            spec = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
            spec["dimensions"]["QUERY"]["contracts"][0]["source_type"] = "ALIYUN::OOS::Template"
            db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
    capture = proof["capture_recovery"]
    if damage == "other-inspector":
        capture["console_inspection"] = "oos_template_metadata"
    elif damage == "new-cloud-call":
        capture["cloud_calls"] = 1
    elif damage == "source-drift":
        json_write(root / proof["sources"]["QUERY"]["path"], {})
    elif damage == "png-drift":
        (root / capture["original_clicks"][scene]["screenshot"]).write_bytes(b"changed")
    elif damage in {"diagnosis-drift", "wrong-diagnosis"}:
        json_write(root / "inputs/R0020/diagnosis.json", {})
        if damage == "wrong-diagnosis":
            capture["remediation_sha256"] = digest(root / "inputs/R0020/diagnosis.json")
    elif damage in {"missing-click", "different-occurrence"}:
        path = root / capture["original_clicks"][scene]["path"]
        value = read_json(path)
        if damage == "missing-click":
            value["actual_ui_click"] = False
        else:
            value["clicked_occurrence"]["focus"] = "another-link"
        json_write(path, value)
        capture["original_clicks"][scene]["sha256"] = digest(path)
    elif damage in {"wire-drift", "unknown-wire", "write-wire"}:
        with database(root / "execution-gate.sqlite3") as db:
            if damage == "wire-drift":
                db.execute("UPDATE grants SET used=2")
            elif damage == "unknown-wire":
                db.execute("UPDATE attempts SET state='unknown'")
            else:
                db.execute("UPDATE grants SET kind='create'")
    elif damage in {"metadata-drift", "no-native-id"}:
        path = root / "packets/original.json"
        value = read_json(path)
        if damage == "metadata-drift":
            value["scenes"][scene]["rationale"]["signed_source_required_fields"]["Description"] = "foreign"
        else:
            value["scenes"][scene]["render_text"] = []
        json_write(path, value)
        proof["packet_sha256"] = digest(path)
    json_write(root / "inputs/review.json", proof)
    if damage == "duplicate":
        store.queue_navigation(original, "inputs/review.json")
    with pytest.raises((WorkflowError, KeyError)):
        store.queue_navigation(original, "inputs/review.json")
