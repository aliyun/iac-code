"""Failure-only import does not relax or impersonate the passed P4 gate."""

import base64
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).parents[2] / "scripts/web/e2e/failed_resource_preflight.py"
    spec = importlib.util.spec_from_file_location("failed_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def paths(tmp_path):
    workspace, session = "/fake/workspace", "own-session"
    key = "ws~" + base64.urlsafe_b64encode(json.dumps([workspace, session]).encode()).decode().rstrip("=")
    files = {
        "plan": tmp_path / "plan.json",
        "ready": tmp_path / "ready.json",
        "create": tmp_path / "create.json",
        "references": tmp_path / re.sub(r"[^A-Za-z0-9-]", "-", workspace) / session / "refs.json",
        "directory": tmp_path / "artifacts",
    }
    now = datetime.now(timezone.utc)
    plan = {
        "planId": "a" * 64,
        "catalogRevisionDigest": "b" * 64,
        "items": [
            {
                "sourceType": "mount",
                "canonicalResourceType": "Mount",
                "receiptSource": "route_catalog",
                "contractId": "route_catalog:mount:aliyun",
                "sourceChainStatus": "passed",
                "sampleLevel": "L2",
                "actualApiCalled": True,
                "product": "nas",
                "action": "DescribeMount",
            }
        ],
    }
    values = {
        "plan": plan,
        "ready": {
            "origin": "http://127.0.0.1:8767",
            "ownSessionKey": key,
            "ready": True,
            "exactVisible": True,
            "ownSessionWasAbsentBefore": True,
        },
        "create": {"origin": "http://127.0.0.1:8767", "submitted": True, "preExistingSidebarKeys": []},
        "references": {
            "references": [
                {
                    "identifier": "domain",
                    "resource_type": "Mount",
                    "region_id": "cn-hangzhou",
                    "console_partition": "aliyun",
                    "evidence": "api_response",
                    "account_scope": "fake-account",
                    "attributes": {"FileSystemId": "fs"},
                    "sources": [
                        {
                            "product": "nas",
                            "action": "DescribeMount",
                            "evidence": "api_response",
                            "tool_use_id": "call-fake",
                            "observed_at": (now - timedelta(seconds=2)).timestamp(),
                        }
                    ],
                }
            ]
        },
    }
    for name, value in values.items():
        files[name].parent.mkdir(parents=True, exist_ok=True)
        files[name].write_text(json.dumps(value), encoding="utf-8")
    files["directory"].mkdir()
    for n, navigation in enumerate(("normal", "shift")):
        raw = {
            "navigation": navigation,
            "actualUiClick": True,
            "loginPassed": True,
            "actualNavigation": "iframe" if not n else "new_tab",
            "expectedNavigation": "iframe" if not n else "new_tab",
            "newTabObserved": bool(n),
            "failureReason": "route_error",
            "routeMatched": False,
            "resourceMatched": False,
            "sourceType": "mount",
            "source": "route_catalog",
            "contractId": "route_catalog:mount:aliyun",
            "resourceIdentifier": "domain",
            "regionId": "cn-hangzhou",
            "resourceParentAttributes": {"FileSystemId": "fs"},
            "capturedAt": (now + timedelta(seconds=n)).isoformat(),
            "diagnostics": {
                "contentReady": True,
                "signals": {k: False for k in ("login", "wrongAccount", "permission", "gateway")},
            },
        }
        p = files["directory"] / f"{'a' * 12}-preflight-{navigation}-mount-{navigation}.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        p.with_suffix(".png").write_bytes(b"\x89PNG\r\n\x1a\nfake-offline")
    return {k: str(v) for k, v in files.items()}


def test_failure_gate_only_validates_failed_exact_artifacts(module, paths):
    gate = module.gate(paths)
    batch = gate["batch"]
    receipt = {
        k: batch[k] for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
    }
    receipt.update(navigation="normal", artifacts=batch["items"][0]["artifacts"], liveDestinationStatus="blocked")
    module.validate_gate(receipt, gate)
    with pytest.raises(ValueError, match="cannot record PASS"):
        module.validate_gate({**receipt, "liveDestinationStatus": "passed"}, gate)
    with pytest.raises(ValueError, match="scope mismatch"):
        module.validate_gate({**receipt, "sessionIdDigest": "0" * 64}, gate)
    Path(paths["ready"]).write_text(Path(paths["ready"]).read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        module.validate_gate(receipt, gate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actualUiClick", False),
        ("loginPassed", False),
        ("resourceMatched", True),
        ("routeMatched", True),
        ("failureReason", None),
        ("actualNavigation", "none"),
        ("resourceIdentifier", "wrong"),
        ("regionId", "cn-beijing"),
    ],
)
def test_reject_missing_click_wrong_scope_or_pass(module, paths, field, value):
    p = next(Path(paths["directory"]).glob("*normal*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw[field] = value
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.gate(paths)


def test_reject_old_session(module, paths):
    p = Path(paths["create"])
    create = json.loads(p.read_text(encoding="utf-8"))
    create["preExistingSidebarKeys"] = [json.loads(Path(paths["ready"]).read_text(encoding="utf-8"))["ownSessionKey"]]
    p.write_text(json.dumps(create), encoding="utf-8")
    with pytest.raises(ValueError, match="own ready"):
        module.gate(paths)


def test_reject_missing_png(module, paths):
    next(Path(paths["directory"]).glob("*normal*.png")).unlink()
    with pytest.raises(FileNotFoundError):
        module.gate(paths)


def test_reject_stale_source(module, paths):
    path = Path(paths["references"])
    refs = json.loads(path.read_text(encoding="utf-8"))
    refs["references"][0]["sources"][0]["observed_at"] -= 3600
    path.write_text(json.dumps(refs), encoding="utf-8")
    with pytest.raises(ValueError, match="fresh UI API source"):
        module.gate(paths)


def test_existing_passed_preflight_schema_still_rejects_failure_gate(module, paths):
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "scripts/web/e2e/resource_link_browser_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(module.gate(paths)))


def test_standard_receipt_writer_records_only_failed_probe(module, paths, tmp_path, monkeypatch):
    directory = Path(__file__).parents[2] / "scripts/web/e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "failure_receipt_runner", directory / "run_resource_link_acceptance.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    signals = {
        k: False for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError")
    }
    for p in Path(paths["directory"]).glob("*.json"):
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw.update(
            landingKind="unknown",
            attemptCount=1,
            finalUrl="https://nasnext.console.aliyun.com/mount",
            expectedNavigationReason="shift_override" if raw["navigation"] == "shift" else "configured_signin_role",
        )
        raw["diagnostics"].update(
            expectedUrl=raw["finalUrl"],
            pageTitle="NAS",
            bodyExcerpt="loaded mount page",
            signals=signals,
            search={
                "attempted": False,
                "performed": False,
                "control": None,
                "query": None,
                "resultText": None,
                "resourceMatched": False,
                "finalUrl": None,
            },
        )
        p.write_text(json.dumps(raw), encoding="utf-8")
    matrix = {"catalogRevision": {"digest": "b" * 64}, "entries": [{"sourceType": "mount", "executions": []}]}
    source = {"actualApiCalled": True, "status": "passed", "sourceEvidenceDigest": "d" * 64, "sampleLevel": "L2"}
    monkeypatch.setattr(runner, "validate_batch_plan", lambda *args: None)
    # The fake catalog isolates this test; receipt, browser schema, scope and
    # immutable-artifact checks still execute through the real writer.
    monkeypatch.setattr(runner, "validate_matrix", lambda *args: None)
    monkeypatch.setattr(runner, "_source_evidence_record", lambda *args: source)
    monkeypatch.setattr(
        runner,
        "_route_cloud_source",
        lambda *args: {
            "regionId": "cn-hangzhou",
            "resourceIdentifierDigests": [runner.hashlib.sha256(b"domain").hexdigest()],
            "parentAttributes": {"FileSystemId": "fs"},
        },
    )
    plan = json.loads(Path(paths["plan"]).read_text(encoding="utf-8"))
    plan["items"][0]["allowedNavigations"] = ["normal", "shift"]
    Path(paths["plan"]).write_text(json.dumps(plan), encoding="utf-8")
    capture = module.gate(paths)
    receipt = runner._receipt_from_browser_observation(capture["batch"], capture["batch"]["items"][0], matrix)
    result, execution = runner.record_execution_receipt(
        matrix, receipt, resume=False, browser_preflight=capture, evidence_root=tmp_path / "evidence"
    )
    assert result == "recorded"
    assert execution["liveDestinationStatus"] == "blocked"
    assert not runner._is_complete_execution(execution)
    assert execution["actualUiClick"] is True
    with pytest.raises(runner.MatrixError):
        runner.validate_execution_receipt({**receipt, "liveDestinationStatus": "passed"}, matrix, capture)
    with pytest.raises(runner.MatrixError, match="registered region/resource"):
        runner.validate_execution_receipt({**receipt, "resourceIdentifierDigest": "0" * 64}, matrix, capture)


@pytest.fixture
def polardb_paths(paths):
    p = Path(paths["plan"])
    plan = json.loads(p.read_text(encoding="utf-8"))
    plan["items"][0].update(
        sourceType="alicloud_polardb_account",
        canonicalResourceType="ALIYUN::POLARDB::Account",
        contractId="route_catalog:alicloud_polardb_account:aliyun",
        product="polardb",
        action="DescribeAccounts",
        routeSourceBinding={"regionId": "cn-hangzhou", "parentAttributes": {"DBClusterId": "cluster-fake"}},
    )
    p.write_text(json.dumps(plan), encoding="utf-8")
    p = Path(paths["references"])
    refs = json.loads(p.read_text(encoding="utf-8"))
    refs["references"][0].update(resource_type="ALIYUN::POLARDB::Account", attributes={"DBClusterId": "cluster-fake"})
    refs["references"][0]["sources"][0].update(product="polardb", action="DescribeAccounts")
    p.write_text(json.dumps(refs), encoding="utf-8")
    for p in Path(paths["directory"]).glob("*.json"):
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw.update(
            sourceType="alicloud_polardb_account",
            contractId="route_catalog:alicloud_polardb_account:aliyun",
            resourceParentAttributes={"DBClusterId": "cluster-fake"},
        )
        if raw["navigation"] == "shift":
            raw.update(
                failureReason=None,
                routeMatched=True,
                resourceMatched=True,
                landingKind="parent_list",
                finalUrl="https://polardb.console.aliyun.com/cn-hangzhou/cluster/cluster-fake/account/user",
            )
            raw["diagnostics"].update(
                expectedUrl=raw["finalUrl"].removesuffix("/user"),
                bodyExcerpt="cluster-fake\n账号名\n状态\n类型\ndomain\n可用\n高权限账号\n修改密码",
            )
            raw["diagnostics"]["signals"].update(wrongVariant=False, missing=False, routeError=False)
        p.write_text(json.dumps(raw), encoding="utf-8")
    return paths


def test_verified_shift_does_not_open_p4_or_accept_normal(module, polardb_paths):
    capture = module.verified_shift_gate(polardb_paths)
    batch = capture["batch"]
    assert len(batch["items"]) == 1
    receipt = {
        k: batch[k] for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
    }
    receipt.update(navigation="shift", artifacts=batch["items"][0]["artifacts"], liveDestinationStatus="passed")
    module.validate_gate(receipt, capture)
    with pytest.raises(ValueError):
        module.validate_gate({**receipt, "navigation": "normal"}, capture)
    with pytest.raises(ValueError):
        module.gate(polardb_paths)
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "scripts/web/e2e/resource_link_browser_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(capture))


@pytest.mark.parametrize(
    "field,value",
    [
        ("actualUiClick", False),
        ("loginPassed", False),
        ("failureReason", "route_error"),
        ("resourceIdentifier", "wrong"),
        ("regionId", "cn-beijing"),
        ("finalUrl", "https://attacker.example/cn-hangzhou/cluster/cluster-fake/account/user"),
        ("resourceParentAttributes", {"DBClusterId": "wrong"}),
    ],
)
def test_verified_shift_rejects_wrong_identity_or_click(module, polardb_paths, field, value):
    p = next(Path(polardb_paths["directory"]).glob("*shift*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw[field] = value
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.verified_shift_gate(polardb_paths)


def test_verified_shift_rejects_global_search_only(module, polardb_paths):
    p = next(Path(polardb_paths["directory"]).glob("*shift*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["diagnostics"]["bodyExcerpt"] = "cluster-fake\n共找到1个domain相关产品\n账号名\n状态\n类型"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="account row"):
        module.verified_shift_gate(polardb_paths)


def test_verified_shift_real_receipt_writer(module, polardb_paths, tmp_path, monkeypatch):
    directory = Path(__file__).parents[2] / "scripts/web/e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "verified_shift_runner", directory / "run_resource_link_acceptance.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    p = next(Path(polardb_paths["directory"]).glob("*shift*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.update(attemptCount=1, expectedNavigationReason="shift_override")
    raw["diagnostics"].update(
        pageTitle="PolarDB",
        search={
            "attempted": False,
            "performed": False,
            "control": None,
            "query": None,
            "resultText": None,
            "resourceMatched": False,
            "finalUrl": None,
        },
    )
    p.write_text(json.dumps(raw), encoding="utf-8")
    p = Path(polardb_paths["plan"])
    plan = json.loads(p.read_text(encoding="utf-8"))
    plan["items"][0]["allowedNavigations"] = ["normal", "shift"]
    p.write_text(json.dumps(plan), encoding="utf-8")
    matrix = {
        "catalogRevision": {"digest": "b" * 64},
        "entries": [{"sourceType": "alicloud_polardb_account", "executions": []}],
    }
    source = {"actualApiCalled": True, "status": "passed", "sourceEvidenceDigest": "d" * 64, "sampleLevel": "L2"}
    monkeypatch.setattr(runner, "validate_batch_plan", lambda *args: None)
    monkeypatch.setattr(runner, "validate_matrix", lambda *args: None)
    monkeypatch.setattr(runner, "_source_evidence_record", lambda *args: source)
    monkeypatch.setattr(
        runner,
        "_route_cloud_source",
        lambda *args: (
            plan["items"][0]["routeSourceBinding"]
            | {"resourceIdentifierDigests": [runner.hashlib.sha256(b"domain").hexdigest()]}
        ),
    )
    capture = module.verified_shift_gate(polardb_paths)
    receipt = runner._receipt_from_browser_observation(capture["batch"], capture["batch"]["items"][0], matrix)
    result, execution = runner.record_execution_receipt(
        matrix, receipt, resume=False, browser_preflight=capture, evidence_root=tmp_path / "evidence"
    )
    assert result == "recorded"
    assert execution["navigation"] == "shift"
    assert execution["liveDestinationStatus"] == "passed"
    assert runner._is_complete_execution(execution)
    with pytest.raises(runner.MatrixError):
        runner.validate_execution_receipt({**receipt, "navigation": "normal"}, matrix, capture)


@pytest.fixture
def ots_paths(paths):
    p = Path(paths["plan"])
    plan = json.loads(p.read_text(encoding="utf-8"))
    plan["items"][0].update(
        sourceType="alicloud_ots_table",
        canonicalResourceType="ALIYUN::OTS::Table",
        contractId="route_catalog:alicloud_ots_table:aliyun",
        receiptSource="route_catalog",
        product="tablestore",
        action="ListTable",
        allowedNavigations=["normal", "shift"],
        routeSourceBinding={
            "regionId": "cn-hangzhou",
            "parentAttributes": {"InstanceName": "instance-fake"},
            "resourceIdentifierDigests": [hashlib.sha256(b"domain").hexdigest()],
        },
    )
    p.write_text(json.dumps(plan), encoding="utf-8")
    p = Path(paths["references"])
    refs = json.loads(p.read_text(encoding="utf-8"))
    refs["references"][0].update(resource_type="ALIYUN::OTS::Table", attributes={"InstanceName": "instance-fake"})
    refs["references"][0]["sources"][0].update(product="tablestore", action="ListTable")
    p.write_text(json.dumps(refs), encoding="utf-8")
    signals = {
        k: False for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError")
    }
    for p in Path(paths["directory"]).glob("*.json"):
        raw = json.loads(p.read_text(encoding="utf-8"))
        normal = raw["navigation"] == "normal"
        route = "/cn-hangzhou/instance-fake/domain/dataManage"
        raw.update(
            canonicalResourceType="ALIYUN::OTS::Table",
            sourceType="alicloud_ots_table",
            contractId="route_catalog:alicloud_ots_table:aliyun",
            resourceParentAttributes={"InstanceName": "instance-fake"},
            attemptCount=1,
            expectedNavigationReason="configured_signin_role" if normal else "shift_override",
            finalUrl="https://" + ("ots4service" if normal else "ots") + ".console.aliyun.com" + route,
            failureReason="permission_denied" if normal else None,
            loginPassed=not normal,
            routeMatched=not normal,
            resourceMatched=True,
            landingKind="unknown" if normal else "exact_detail",
        )
        raw["diagnostics"].update(
            expectedUrl="https://ots.console.aliyun.com" + route,
            contentReady=not normal,
            signals={**signals, "permission": normal},
            pageTitle="Tablestore",
            bodyExcerpt="fake offline header",
            search={
                "attempted": False,
                "performed": False,
                "control": None,
                "query": None,
                "resultText": None,
                "resourceMatched": False,
                "finalUrl": None,
            },
        )
        p.write_text(json.dumps(raw), encoding="utf-8")
    return paths


def test_ots_mixed_gate_preserves_originals_and_does_not_open_p4(module, ots_paths):
    before = {p: p.read_bytes() for p in Path(ots_paths["directory"]).glob("*")}
    capture = module.ots_table_gate(ots_paths)
    batch = capture["batch"]
    assert len(batch["items"]) == 2
    assert batch["items"][0]["failureReason"] == "permission_denied"
    assert batch["items"][0]["loginPassed"] is False
    for item in batch["items"]:
        receipt = {
            k: batch[k] for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
        }
        receipt.update(
            navigation=item["navigation"],
            artifacts=item["artifacts"],
            liveDestinationStatus="blocked" if item["navigation"] == "normal" else "passed",
        )
        module.validate_gate(receipt, capture)
        with pytest.raises(ValueError, match="original verdict"):
            module.validate_gate(
                {**receipt, "liveDestinationStatus": "passed" if item["navigation"] == "normal" else "blocked"}, capture
            )
    assert all(p.read_bytes() == content for p, content in before.items())
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "scripts/web/e2e/resource_link_browser_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(capture))
    with pytest.raises(ValueError):
        module.gate(ots_paths)
    with pytest.raises(ValueError):
        module.verified_shift_gate(ots_paths)


@pytest.mark.parametrize(
    "navigation,field,value",
    [
        ("shift", "actualUiClick", False),
        ("shift", "loginPassed", False),
        ("shift", "routeMatched", False),
        ("shift", "resourceMatched", False),
        ("shift", "failureReason", "permission_denied"),
        ("shift", "landingKind", "parent_list"),
        ("shift", "newTabObserved", False),
        ("shift", "resourceIdentifier", "wrong"),
        ("shift", "regionId", "cn-beijing"),
        ("shift", "resourceParentAttributes", {"InstanceName": "wrong"}),
        ("shift", "finalUrl", "https://attacker.example/cn-hangzhou/instance-fake/domain/dataManage"),
        ("normal", "actualUiClick", False),
        ("normal", "loginPassed", True),
        ("normal", "routeMatched", True),
        ("normal", "resourceMatched", False),
        ("normal", "failureReason", "login_error"),
        ("normal", "canonicalResourceType", "Mount"),
        ("normal", "actualNavigation", "new_tab"),
        ("normal", "attemptCount", 2),
    ],
)
def test_ots_mixed_rejects_wrong_identity_flags_or_navigation(module, ots_paths, navigation, field, value):
    p = next(Path(ots_paths["directory"]).glob(f"*{navigation}*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw[field] = value
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.ots_table_gate(ots_paths)


@pytest.mark.parametrize(
    "navigation,field,value",
    [
        ("shift", "contentReady", False),
        ("normal", "contentReady", True),
        ("shift", "signals", {"permission": True}),
        ("normal", "signals", {"permission": False}),
        ("shift", "expectedUrl", "https://ots.console.aliyun.com/cn-hangzhou/wrong/domain/dataManage"),
    ],
)
def test_ots_mixed_rejects_incomplete_or_changed_diagnostics(module, ots_paths, navigation, field, value):
    p = next(Path(ots_paths["directory"]).glob(f"*{navigation}*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["diagnostics"][field] = value
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.ots_table_gate(ots_paths)


def test_ots_mixed_real_writer_records_failure_and_pass_separately(module, ots_paths, tmp_path, monkeypatch):
    directory = Path(__file__).parents[2] / "scripts/web/e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("ots_mixed_writer", directory / "run_resource_link_acceptance.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    matrix = {
        "catalogRevision": {"digest": "b" * 64},
        "entries": [{"sourceType": "alicloud_ots_table", "executions": []}],
    }
    plan = json.loads(Path(ots_paths["plan"]).read_text(encoding="utf-8"))
    source = {"actualApiCalled": True, "status": "passed", "sourceEvidenceDigest": "d" * 64, "sampleLevel": "L2"}
    monkeypatch.setattr(runner, "validate_batch_plan", lambda *args: None)
    monkeypatch.setattr(runner, "validate_matrix", lambda *args: None)
    monkeypatch.setattr(runner, "_source_evidence_record", lambda *args: source)
    monkeypatch.setattr(runner, "_route_cloud_source", lambda *args: plan["items"][0]["routeSourceBinding"])
    capture = module.ots_table_gate(ots_paths)
    for item in capture["batch"]["items"]:
        receipt = runner._receipt_from_browser_observation(capture["batch"], item, matrix)
        result, execution = runner.record_execution_receipt(
            matrix,
            receipt,
            resume=False,
            browser_preflight=capture,
            evidence_root=tmp_path / "evidence",
        )
        assert result == "recorded"
        assert runner._is_complete_execution(execution) is (item["navigation"] == "shift")
        if item["navigation"] == "normal":
            assert execution["failureReason"] == "permission_denied"
            assert execution["liveDestinationStatus"] == "blocked"
        else:
            assert execution["liveDestinationStatus"] == "passed"
