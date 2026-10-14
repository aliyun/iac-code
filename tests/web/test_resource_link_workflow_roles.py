"""Plan-scoped role identity; all models and evidence are offline fixtures."""

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, read_json
from scripts.web.e2e.resource_link_workflow.store import Store
from tests.web.test_resource_link_workflow import reserve, resource


def test_new_plan_gets_new_sol_and_existing_plan_keeps_original(tmp_path):
    store = Store.initialize(tmp_path / "state")
    store.import_plan({"schema_version": 1, "plan_id": "first", "resources": [resource(1), resource(2)]})
    store.pause(False)
    first = reserve(store, "prepare")
    token = read_json(store.root / "jobs" / first["job_id"] / "capability.json")["token"]
    store.bind(first["job_id"], token, "first-sol", "first-turn")
    # Model terminal/preparation state fixtures, not live acceptance or PASS.
    with store.transaction() as con:
        con.execute("UPDATE jobs SET state='complete'")
        con.execute("UPDATE tasks SET state='complete' WHERE resource_id='R0001'")
    second = reserve(store, "prepare")
    with store.transaction() as con:
        assert con.execute("SELECT thread_id FROM jobs WHERE id=?", (second["job_id"],)).fetchone()[0] == "first-sol"
    token2 = read_json(store.root / "jobs" / second["job_id"] / "capability.json")["token"]
    with pytest.raises(WorkflowError, match="rebind"):
        store.bind(second["job_id"], token2, "replacement-sol", "second-turn")
    with store.transaction() as con:
        con.execute("UPDATE jobs SET state='complete'")
        con.execute("UPDATE tasks SET state='complete'")
        con.execute("UPDATE meta SET value='legacy-global-sol' WHERE key='sol_thread'")
    store.import_plan({"schema_version": 1, "plan_id": "new", "resources": [resource(3)]})
    new = reserve(store, "prepare")
    with store.transaction() as con:
        assert con.execute("SELECT thread_id FROM jobs WHERE id=?", (new["job_id"],)).fetchone()[0] is None
    new_token = read_json(store.root / "jobs" / new["job_id"] / "capability.json")["token"]
    store.bind(new["job_id"], new_token, "new-sol", "new-turn")
    with store.transaction() as con:
        assert store.plan_sol_thread(con, "first") == "first-sol"
        assert store.plan_sol_thread(con, "new") == "new-sol"
        assert con.execute("SELECT value FROM meta WHERE key='sol_thread'").fetchone()[0] == "legacy-global-sol"
        # Legacy plans without a per-run meta record recover from their own jobs.
        con.execute("DELETE FROM meta WHERE key='sol_thread:first'")
        assert store.plan_sol_thread(con, "first") == "first-sol"
        con.execute("UPDATE jobs SET thread_id='conflict' WHERE id=?", (second["job_id"],))
        with pytest.raises(WorkflowError, match="conflicting Sol"):
            store.plan_sol_thread(con, "first")
