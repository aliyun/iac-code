"""Report-only offline fixtures: no cloud, browser, model or acceptance writes."""

import copy
import json
from html.parser import HTMLParser

import pytest

from scripts.web.e2e.resource_link_workflow import reports
from scripts.web.e2e.resource_link_workflow.files import digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.store import Store


@pytest.fixture
def report_store(tmp_path):
    store = Store.initialize(tmp_path / "state")
    catalog = reports.DATA_DIRECTORY / "catalog.json"
    spec = next(item for item in read_json(catalog)["resources"] if item["id"] == "R0033")
    store.import_plan({"schema_version": 1, "plan_id": "offline", "resources": [spec]})
    return store


def add_creation(store, status):
    json_write(store.root / "source.json", {"offline": True})
    (store.root / "creation.png").write_bytes(b"offline fixture, not an actual screenshot")
    json_write(store.root / "creation.json", {"screenshot": "creation.png"})
    with store.transaction() as db:
        db.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (
                "offline:R0033",
                "R0033-TF-creation",
                status,
                json.dumps(
                    {
                        "reason": "offline <creation> receipt",
                        "evidence": "creation.json",
                        "source_evidence": "source.json",
                    }
                ),
                1,
            ),
        )


def test_creation_is_separate_from_unchanged_historical_navigation(report_store, tmp_path, monkeypatch):
    before = []
    original = reports.document
    monkeypatch.setattr(
        reports, "document", lambda title, groups: before.extend(copy.deepcopy(groups)) or original(title, groups)
    )
    reports.render_results(report_store, tmp_path / "before.html")
    add_creation(report_store, "pass")
    after = []
    monkeypatch.setattr(
        reports, "document", lambda title, groups: after.extend(copy.deepcopy(groups)) or original(title, groups)
    )
    target = tmp_path / "after.html"
    baseline = digest(reports.DATA_DIRECTORY / "historical-results.json")
    reports.render_results(report_store, target)
    old = next(
        row for g in before if g["key"] == "ALIYUN::OSS::Bucket" for row in g["rows"] if row["dimension"] == "TF"
    )
    new = next(row for g in after if g["key"] == "ALIYUN::OSS::Bucket" for row in g["rows"] if row["dimension"] == "TF")
    assert new["status"] == old["status"]
    assert new["creation"]["status"] == "pass" and not new.get("ros_managed_modes")
    value = target.read_text(encoding="utf-8")
    assert '<th data-mode="creation">创建验收</th><th>清理</th>' in value
    assert "创建：ROS 托管 Terraform；点击：沿用历史入口结果，未复测。" in value
    assert 'href="state/creation.png"' in value
    assert "&lt;creation&gt;" in value
    assert "不适用 · 只读" in value
    assert digest(reports.DATA_DIRECTORY / "historical-results.json") == baseline


def test_execution_gap_is_not_hidden_and_cannot_replace_retained_creation_pass(report_store, tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or title)
    add_creation(report_store, "execution_gap")
    reports.render_results(report_store, tmp_path / "gap.html")
    row = next(
        row for g in captured if g["key"] == "ALIYUN::OSS::Bucket" for row in g["rows"] if row["dimension"] == "TF"
    )
    assert row["creation"]["status"] == "execution_gap"
    captured.clear()
    add_creation(report_store, "pass")
    add_creation(report_store, "execution_gap")
    reports.render_results(report_store, tmp_path / "retained.html")
    row = next(
        row for g in captured if g["key"] == "ALIYUN::OSS::Bucket" for row in g["rows"] if row["dimension"] == "TF"
    )
    assert row["creation"]["status"] == "pass"


def owner_fixture(root, state="deleted"):
    resource = {"id": "offline-bucket"}
    json_write(root / "source.json", {"offline": True})
    proof = {
        "ownership_id": "owner",
        "resource": resource,
        "absent": True,
        "failed": False,
        "source_evidence": "source.json",
        "source_sha256": digest(root / "source.json"),
    }
    json_write(root / "proof.json", proof)
    owner = {
        "id": "owner",
        "task_id": "offline:R0033",
        "state": state,
        "resource": json.dumps(resource),
        "evidence": json.dumps({"path": "proof.json", "sha256": digest(root / "proof.json")}),
    }
    return owner, proof


def test_cleanup_requires_unchanged_bound_proof_and_accounts_for_outstanding_owner(tmp_path):
    owner, _ = owner_fixture(tmp_path)
    target = tmp_path / "report.html"
    assert reports.cleanup_summary(tmp_path, [owner], target)["status"] == "pass"
    assert reports.cleanup_summary(tmp_path, [owner, {**owner, "state": "present"}], target)["status"] != "pass"
    json_write(tmp_path / "source.json", {"changed": True})
    assert reports.cleanup_summary(tmp_path, [owner], target)["status"] == "review"


def test_pending_delete_is_authorized_disposition_not_physical_absence(tmp_path, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import pending_delete

    owner, proof = owner_fixture(tmp_path)
    proof.update(kind="ops_user_accepted_pending_delete", absent=False)
    json_write(tmp_path / "proof.json", proof)
    owner["evidence"] = json.dumps({"path": "proof.json", "sha256": digest(tmp_path / "proof.json")})
    calls = []
    monkeypatch.setattr(pending_delete, "validate", lambda *args: calls.append(args))
    result = reports.cleanup_summary(tmp_path, [owner], tmp_path / "report.html")
    assert len(calls) == 1
    assert result["status"] != "pass"
    assert "PendingDelete" in result["label"] and "不代表已物理消失" in result["note"]


def test_artifact_links_cannot_escape_state_or_embed_external_urls(tmp_path):
    assert reports.evidence_link(tmp_path, tmp_path / "report.html", "../outside", "bad") == ""
    assert reports.evidence_link(tmp_path, tmp_path / "report.html", "https://secret.invalid/token", "bad") == ""
    json_write(tmp_path / "with space.json", {})
    assert 'href="with%20space.json"' in reports.evidence_link(
        tmp_path,
        tmp_path / "report.html",
        "with space.json",
        "source",
    )


def test_matrix_keeps_original_six_columns_and_does_not_claim_creation(tmp_path):
    directory = reports.DATA_DIRECTORY
    target = tmp_path / "matrix.html"
    reports.render_matrix(directory / "catalog.json", target)
    assert "<th>创建验收</th>" not in target.read_text(encoding="utf-8")


def targeted_report_store(tmp_path):
    store = Store.initialize(tmp_path / "state")
    catalog = reports.DATA_DIRECTORY / "catalog.json"
    spec = copy.deepcopy(next(item for item in read_json(catalog)["resources"] if item["id"] == "R0020"))
    spec["retest_scope"] = {"kind": "failed_navigation_existing", "scene_ids": ["R0020-ROS-1-open"]}
    store.import_plan({"schema_version": 1, "plan_id": "source-check", "resources": [spec]})
    with store.transaction() as db:
        db.execute(
            "UPDATE tasks SET state='blocked',reason=? WHERE id=?",
            (
                "ROS sample deleted <source>; no new browser test",
                "source-check:R0020",
            ),
        )
    return store, spec


def test_targeted_source_blocker_explains_only_pending_branch_without_new_verdict(tmp_path, monkeypatch):
    store, _ = targeted_report_store(tmp_path)
    captured = []
    original = reports.document
    monkeypatch.setattr(
        reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or original(title, groups)
    )
    baseline = digest(reports.DATA_DIRECTORY / "historical-results.json")
    target = tmp_path / "blocked.html"
    reports.render_results(store, target)
    rows = next(group["rows"] for group in captured if group["key"] == "ALIYUN::ECS::SNatEntry")
    ros = next(row for row in rows if row["dimension"] == "ROS")
    assert ros["status"] == {"iframe": "pass", "open": "fail"}
    assert "当前复测阻塞｜source-check｜直接打开" in ros["reason"]
    assert "ROS sample deleted <source>" in ros["reason"]
    assert all("当前复测阻塞" not in row["reason"] for row in rows if row["dimension"] != "ROS")
    assert "&lt;source&gt;" in target.read_text(encoding="utf-8")
    assert "deleted <source>" not in target.read_text(encoding="utf-8")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert digest(reports.DATA_DIRECTORY / "historical-results.json") == baseline


def test_blocked_task_cannot_attach_pending_diagnosis_to_an_accepted_scene(tmp_path, monkeypatch):
    store, _ = targeted_report_store(tmp_path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (
                "source-check:R0020",
                "R0020-ROS-1-open",
                "pass",
                json.dumps({"reason": "own accepted scene"}),
                1,
            ),
        )
    captured = []
    monkeypatch.setattr(reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or title)
    reports.render_results(store, tmp_path / "positive.html")
    row = next(
        row
        for group in captured
        if group["key"] == "ALIYUN::ECS::SNatEntry"
        for row in group["rows"]
        if row["dimension"] == "ROS"
    )
    assert row["status"] == {"iframe": "pass", "open": "pass"}
    assert "当前复测阻塞" not in row["reason"] and "own accepted scene" in row["reason"]


def test_older_targeted_blocker_is_not_projected_into_a_newer_plan(tmp_path, monkeypatch):
    store, spec = targeted_report_store(tmp_path)
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='complete' WHERE id='source-check:R0020'")
    store.import_plan({"schema_version": 1, "plan_id": "next-source-check", "resources": [spec]})
    captured = []
    monkeypatch.setattr(reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or title)
    reports.render_results(store, tmp_path / "new-plan.html")
    row = next(
        row
        for group in captured
        if group["key"] == "ALIYUN::ECS::SNatEntry"
        for row in group["rows"]
        if row["dimension"] == "ROS"
    )
    assert "当前复测阻塞" not in row["reason"] and "ROS sample deleted" not in row["reason"]


class ReportParser(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tabs, self.panels, self.metrics, self.resources, self.filters, self.rows = [], [], [], [], [], []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if item.get("role") == "tab":
            self.tabs.append(item)
        elif item.get("role") == "tabpanel":
            self.panels.append(item)
        if item.get("class") == "metric":
            self.metrics.append(item)
        if item.get("class") == "filter-button":
            self.filters.append(item)
        if tag == "tr" and "data-dimension" in item:
            self.rows.append(item)
        if tag == "tbody" and "data-search" in item:
            self.resources.append(item)


def report_group(service, name, statuses):
    return {
        "service": service,
        "name": name,
        "key": name,
        "rows": [
            {
                "dimension": "QUERY",
                "source_type": "API",
                "status": {"iframe": iframe, "open": opened},
                "reason": "offline fixture",
                "creation": {"status": "pass", "label": "creation only"},
                "cleanup": {"status": "review", "label": "PendingDelete"},
            }
            for iframe, opened in statuses
        ],
    }


def test_results_tabs_group_products_and_keep_accessible_selected_panel():
    value = reports.document(
        "Report",
        [
            report_group("VPC", "VSwitch", [("pass", "fail"), ("unsupported", "pass")]),
            report_group("ECS", "Instance", [("untested", "review")]),
            report_group("VPC", "VPC", [("execution_gap", "product_gap"), ("no_sample", "untested")]),
        ],
    )
    parsed = ReportParser(value)
    assert len(parsed.tabs) == len(parsed.panels) == 2
    assert [tab["aria-selected"] for tab in parsed.tabs] == ["true", "false"]
    assert [tab["tabindex"] for tab in parsed.tabs] == ["0", "-1"]
    for tab, panel in zip(parsed.tabs, parsed.panels):
        assert tab["aria-controls"] == panel["id"] and panel["aria-labelledby"] == tab["id"]
        assert ("hidden" not in panel) == (tab["aria-selected"] == "true")
    assert len(parsed.resources) == 3
    assert 'rowspan="2" class="resource">VSwitch</th>' in value
    assert value.count("<table>") == 2 and "<th>服务</th>" not in value
    assert "创建与清理不计入这些数量" in value
    vpc = value.split('id="product-1"', 1)[1].split("</section>", 1)[0]
    expected = {"pass": 2, "fail": 2, "untested": 1, "unsupported": 1, "execution_gap": 1, "review": 0, "no_sample": 1}
    for status, count in expected.items():
        marker = f'data-status="{status}"'
        assert f"<strong>{count}</strong>" in vpc.split(marker, 1)[1].split("</div>", 1)[0]
    assert "未通过 1 项；产品缺陷 1 项" in vpc
    assert "8 项点击验收" in vpc


def test_report_tabs_escape_product_names_and_filter_whole_resource_groups():
    value = reports.document("<Report>", [report_group('X"><script>bad</script>', 'A"<&', [("pass", "pass")])])
    parsed = ReportParser(value)
    assert len(parsed.tabs) == len(parsed.panels) == 1
    assert "<script>bad</script>" not in value and "&lt;Report&gt;" in value
    assert parsed.resources[0]["data-search"] == 'x"><script>bad</script> a"<& a"<&'
    assert "tbody[data-search]" in value and "ArrowRight" in value and "Home" in value


def test_empty_report_has_no_tabs_or_fake_counts():
    value = reports.document("Report", [])
    parsed = ReportParser(value)
    assert not parsed.tabs and not parsed.panels and not parsed.metrics
    assert "暂无验收结果" in value


def test_report_has_three_composable_filters_defaulting_to_all():
    value = reports.document("Report", [report_group("VPC", "VSwitch", [("pass", "fail")])])
    parsed = ReportParser(value)
    choices = {key: [b for b in parsed.filters if b["data-filter"] == key] for key in ("source", "mode", "status")}
    assert [b["data-value"] for b in choices["source"]] == ["all", "ROS", "TF", "CREATE", "QUERY"]
    assert [b["data-value"] for b in choices["mode"]] == ["all", "iframe", "open", "creation"]
    assert [b["data-value"] for b in choices["status"]] == [
        "all",
        "pass",
        "fail",
        "untested",
        "unsupported",
        "execution_gap",
        "review",
        "no_sample",
    ]
    for buttons in choices.values():
        assert [b["data-value"] for b in buttons if b["aria-pressed"] == "true"] == ["all"]
    assert parsed.rows[0]["data-iframe"] == "pass" and parsed.rows[0]["data-open"] == "fail"
    assert parsed.rows[0]["data-creation"] == ""  # QUERY is not an untested creation.
    assert 'aria-label="当前筛选总计"' in value and "当前筛选总计 · 所有服务" in value
    for field in ("services", "resources", "sources", "clicks", "creations"):
        assert value.count(f'data-total="{field}"') == 1


def test_creation_statistics_deduplicate_contracts_without_crediting_clicks():
    group = report_group("VPC", "VSwitch", [("untested", "fail"), ("untested", "pass"), ("untested", "untested")])
    group["rows"][0]["dimension"] = group["rows"][1]["dimension"] = "ROS"
    group["rows"][2]["dimension"] = "TF"
    group["rows"][2]["creation"]["status"] = "product_gap"
    value = reports.document("Report", [group])
    assert "6 项点击验收 · 2 项创建验收" in value
    passed = value.split('class="metric" data-status="pass"', 1)[1].split("</div>", 1)[0]
    failed = value.split('class="metric" data-status="fail"', 1)[1].split("</div>", 1)[0]
    assert "<strong>1</strong>" in passed and "创建<b>1</b>" in passed
    assert "<strong>1</strong>" in failed and "创建<b>1</b>" in failed
    assert sum(row["data-iframe"] == "untested" for row in ReportParser(value).rows) == 3


def test_cumulative_report_retains_history_when_retest_has_no_verdict(tmp_path, monkeypatch):
    store, _ = targeted_report_store(tmp_path)
    captured = []
    original = reports.document
    monkeypatch.setattr(
        reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or original(title, groups)
    )
    target = tmp_path / "current.html"
    reports.render_results(store, target)
    row = next(
        row
        for group in captured
        if group["key"] == "ALIYUN::ECS::SNatEntry"
        for row in group["rows"]
        if row["dimension"] == "ROS"
    )
    assert row["status"] == {"iframe": "pass", "open": "fail"}
    value = target.read_text(encoding="utf-8")
    assert 'class="badge fail">× 未通过' in value
    assert "本轮复测" not in value and "run-summary" not in value


def test_cumulative_report_merges_latest_accepted_result_without_run_summary(tmp_path, monkeypatch):
    store, spec = targeted_report_store(tmp_path)
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='complete' WHERE id='source-check:R0020'")
        db.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (
                "source-check:R0020",
                "R0020-ROS-1-open",
                "fail",
                json.dumps({"reason": "old fail"}),
                1,
            ),
        )
    store.import_plan({"schema_version": 1, "plan_id": "latest", "resources": [spec]})
    with store.transaction() as db:
        for status in ("fail", "pass"):
            db.execute(
                "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
                (
                    "latest:R0020",
                    "R0020-ROS-1-open",
                    status,
                    json.dumps({"reason": status}),
                    2,
                ),
            )
    captured = []
    original = reports.document
    monkeypatch.setattr(
        reports, "document", lambda title, groups: captured.extend(copy.deepcopy(groups)) or original(title, groups)
    )
    target = tmp_path / "latest.html"
    reports.render_results(store, target)
    row = next(
        row
        for group in captured
        if group["key"] == "ALIYUN::ECS::SNatEntry"
        for row in group["rows"]
        if row["dimension"] == "ROS"
    )
    assert row["status"] == {"iframe": "pass", "open": "pass"}
    assert "本轮复测" not in target.read_text(encoding="utf-8")
