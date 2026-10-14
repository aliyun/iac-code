"""Keep resource-link regression data separate from local cloud acceptance state."""

from pathlib import Path

import pytest

from tests.web.resource_link_data import workflow_data


@pytest.fixture
def offline_workflow_data(tmp_path):
    return workflow_data(tmp_path / "synthetic-workflow-data")


@pytest.fixture(autouse=True)
def _isolated_workflow_reports(request, monkeypatch):
    names = {
        "test_resource_link_workflow",
        "test_resource_link_workflow_reports",
        "test_resource_link_workflow_remaining_review",
        "test_resource_link_workflow_retest",
        "test_resource_link_workflow_plan_handoff",
    }
    if request.module.__name__.rsplit(".", 1)[-1] not in names:
        return
    from scripts.web.e2e.resource_link_workflow import remaining_review, reports, store

    directory = request.getfixturevalue("offline_workflow_data")
    # Only filesystem inputs are rebound; the actual report/review implementations
    # and all validation, audit and acceptance decisions remain the production code.
    monkeypatch.setattr(reports, "DATA_DIRECTORY", directory)
    monkeypatch.setattr(remaining_review, "DATA_DIRECTORY", directory)
    monkeypatch.setattr(store, "DATA_DIRECTORY", directory)
    monkeypatch.setattr(request.module, "WORKFLOW_DATA", directory, raising=False)
    if request.module.__name__.rsplit(".", 1)[-1] == "test_resource_link_workflow_plan_handoff":
        from tests.web import test_resource_link_workflow_remaining_review as shared_fixtures

        monkeypatch.setattr(shared_fixtures, "DIRECTORY", directory)
    for name in ("CATALOG", "DIRECTORY"):
        value = getattr(request.module, name, None)
        if isinstance(value, Path) and "resource_link_workflow" in value.parts:
            monkeypatch.setattr(request.module, name, directory / value.name if name == "CATALOG" else directory)
