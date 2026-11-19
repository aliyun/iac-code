"""Execute the production reducer; progress cannot attach to a different input."""

import shutil

import pytest

from tests.web.test_frontend_static import _run_events_script


@pytest.mark.parametrize("canceled", [False, True])
def test_turn_done_removes_only_unfinished_wiki_summaries(tmp_path, canceled):
    result = _run_events_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        const wiki = (phase, turnId) => ({queryId: phase + turnId, phase, turnId});
        const original = { messages: {
          user: {role: "user", text: "question", wikiSearch: wiki("extracting", "t1")},
          step: {role: "assistant", pipelineStep: {wikiSearch: wiki("searching")}},
          pending: {role: "user", wikiSearch: wiki("pending", "t1")},
          ready: {role: "user", wikiSearch: wiki("ready", "t1")},
          skipped: {role: "user", wikiSearch: wiki("skipped", "t1")},
          other: {role: "user", wikiSearch: wiki("extracting", "t2")}
        }};
        const state = reduceEvent(original, {type: "turn.done", sequence: 10,
          payload: {turnId: "t1", canceled: __CANCELED__}});
        console.log(JSON.stringify({state, original}));
        """.replace("__CANCELED__", str(canceled).lower()),
    )
    messages = result["state"]["messages"]
    assert "wikiSearch" not in messages["user"] and messages["user"]["text"] == "question"
    assert "wikiSearch" not in messages["step"]["pipelineStep"]
    assert "wikiSearch" not in messages["pending"]
    assert messages["ready"]["wikiSearch"]["phase"] == "ready"
    assert messages["skipped"]["wikiSearch"]["phase"] == "skipped"
    assert messages["other"]["wikiSearch"]["phase"] == "extracting"
    assert result["original"]["messages"]["user"]["wikiSearch"]["phase"] == "extracting"


def test_wiki_progress_updates_one_owner_without_synthetic_messages(tmp_path):
    assert shutil.which("node"), "Wiki UI contracts require Node"
    result = _run_events_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        let state = { messages: { old: {role: "user", text: "older"}, current: {role: "user", text: "current"} } };
        state = reduceEvent(state, {type: "context.wiki_search", sequence: 1,
            payload: {queryId: "q1", ownerMessageId: "old", phase: "extracting", summary: "searching"}});
        state = reduceEvent(state, {type: "context.wiki_search", sequence: 2,
            payload: {queryId: "q1", ownerMessageId: "old", phase: "ready", summary: "one file"}});
        state = reduceEvent(state, {type: "context.wiki_search", sequence: 3,
            payload: {queryId: "missing", ownerMessageId: "absent", phase: "ready", summary: "unrelated"}});
        console.log(JSON.stringify(state));
    """,
    )
    assert set(result["messages"]) == {"old", "current"}
    assert result["messages"]["old"]["wikiSearch"]["phase"] == "ready"
    assert "wikiSearch" not in result["messages"]["current"]
    assert result["messages"]["old"]["text"] == "older"


def test_pipeline_wiki_updates_existing_step_without_creating_an_assistant_answer(tmp_path):
    result = _run_events_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        let state = reduceEvent({}, {type: "pipeline.step.marker", sequence: 1,
          payload: {markerId: "step1", kind: "pipeline_step", content: "Step1",
                    pipelineStep: {groupId: "step1", status: "working"}}});
        state = reduceEvent(state, {type: "context.wiki_search", sequence: 2,
          payload: {ownerMessageId: "step1", queryId: "q1", phase: "ready", summary: "one file"}});
        state = reduceEvent(state, {type: "pipeline.step.marker", sequence: 3,
          payload: {markerId: "step1", kind: "pipeline_step", content: "Step1",
                    pipelineStep: {groupId: "step1", status: "completed"}}});
        console.log(JSON.stringify(state));
    """,
    )
    assert list(result["messages"]) == ["step1"]
    assert result["messages"]["step1"]["wikiSearch"]["phase"] == "ready"
    assert result["messages"]["step1"]["text"] == "Step1"
