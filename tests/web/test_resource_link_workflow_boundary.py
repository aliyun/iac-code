"""The E2E launcher explicitly installs policy; archives are never dependencies."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iac_code.services import execution_policy
from scripts.web.e2e import qwen_batch_guard
from scripts.web.e2e.resource_link_workflow import execution_gate, web_entry
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, read_json
from scripts.web.e2e.resource_link_workflow.store import Store


@pytest.mark.parametrize(("journal", "slot"), [(None, None), ("", "0"), ("journal", None), ("journal", "wrong")])
def test_workflow_web_launcher_rejects_missing_binding(monkeypatch, journal, slot):
    monkeypatch.setattr(execution_policy, "_policy", None)
    if journal is not None:
        monkeypatch.setenv(execution_gate.ENV, journal)
    if slot is not None:
        monkeypatch.setenv(execution_gate.SLOT_ENV, slot)
    with pytest.raises(execution_gate.GateDeniedError):
        web_entry.main()
    assert execution_policy._policy is None


def test_workflow_web_launcher_installs_before_product_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_policy, "_policy", None)
    monkeypatch.setenv(execution_gate.ENV, str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv(execution_gate.SLOT_ENV, "0")
    app = Mock(side_effect=lambda: execution_policy.check("bash", {}, "session"))
    monkeypatch.setitem(sys.modules, "iac_code.cli.main", SimpleNamespace(app=app))
    web_entry.main()
    app.assert_called_once_with()
    assert execution_policy._policy is execution_gate
    assert execution_policy.check("bash", {}, "session") == execution_gate.ERROR


def test_legacy_queue_has_no_implicit_archive_fallback(tmp_path):
    with pytest.raises(qwen_batch_guard.GuardError, match="explicit --task-dir"):
        qwen_batch_guard.load_queue(tmp_path / "missing", tmp_path / "missing-summary.md")


@pytest.mark.parametrize("lease_state", [None, "closed", "prepared", "bound", "delivered", "audited_unreplayable"])
def test_claim_checks_durable_old_leases_without_task_documents(monkeypatch, tmp_path, capsys, lease_state):
    from scripts.web.e2e.resource_link_workflow import __main__ as cli

    repo, root = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    Store.initialize(root)
    if lease_state:
        folder = repo / "evidence/formal/supervision/qwen-batch-leases"
        folder.mkdir(parents=True)
        (folder / "original.json").write_text(json.dumps({"state": lease_state}), encoding="utf-8")
    monkeypatch.setattr(cli, "Runtime", lambda *_: SimpleNamespace(config={"repo": str(repo)}))
    monkeypatch.setattr(sys, "argv", ["workflow", "--state", str(root), "claim"])
    if lease_state not in {None, "closed"}:
        with pytest.raises(WorkflowError, match="active leases"):
            cli.main()
        assert not (repo / "evidence/resource-link-workflow-owner.json").exists()
    else:
        cli.main()
        assert json.loads(capsys.readouterr().out)["legacy_dispatch_disabled"]
        assert read_json(repo / "evidence/resource-link-workflow-owner.json")["state"] == str(root)
