"""Execute the production reducer; progress cannot attach to a different input."""

import json
import shutil
from pathlib import Path

import pytest

from tests.web.test_frontend_static import _run_events_script


@pytest.mark.parametrize(
    "phase",
    ["extracting", "searching", "ready", "fallback_ready", "no_matches", "skipped", "failed", "canceled", "pending"],
)
def test_wiki_card_shows_progress_sources_and_duration_as_plain_text(tmp_path, phase):
    source = (Path(__file__).parents[2] / "src/iac_code/web/static/js/app.js").read_text(encoding="utf-8")
    renderer = (
        "function buildWikiSearchSummary("
        + source.split("function buildWikiSearchSummary(", 1)[1].split("\nfunction renderConversationMessage(", 1)[0]
    )
    result = _run_events_script(
        tmp_path,
        """
        class Element {
          constructor(tag) {this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {};}
          append(...children) {this.children.push(...children);}
          setAttribute(key, value) {this.attributes[key] = value;}
          get childElementCount() {return this.children.length;}
          set innerHTML(_) {throw new Error('Wiki data must not be inserted as HTML');}
        }
        const document = {createElement: tag => new Element(tag)};
        const t = (key, params = {}) => {
          let value = ({'Search keywords': '检索关键词', 'Matched documents': '命中文档'})[key] || key;
          for (const [name, replacement] of Object.entries(params)) {
            value = value.replace('{' + name + '}', replacement);
          }
          return value;
        };
        const applySpinPhase = () => {};
        __RENDERER__
        const card = buildWikiSearchSummary({queryId: 'q1', phase: __PHASE__, summary: 'Wiki · state',
          elapsedMs: 1240, keywords: ['<img src=x onerror=alert(1)>', 'ECS'],
          files: [{title: '<script>untrusted</script>', path: 'clouds/aliyun/ecs.md', lines:[
            {number:42, text:'**ECS** <img src=x onerror=alert(1)> [link](https://invalid.example)'}]}]});
        console.log(JSON.stringify(card));
        """.replace("__RENDERER__", renderer).replace("__PHASE__", json.dumps(phase)),
    )
    assert result["dataset"] == {"openKey": "wiki-q1", "phase": phase}
    assert result["attributes"]["aria-busy"] == str(phase in {"extracting", "searching"}).lower()
    summary, content = result["children"]
    icon, label = summary["children"]
    assert ("thread-spinner" in icon["className"]) is (phase in {"extracting", "searching"})
    assert label["attributes"]["role"] == "status"
    expected = {
        "extracting": "Wiki search in progress",
        "searching": "Wiki search in progress",
        "ready": "Wiki search completed",
        "fallback_ready": "Wiki search completed",
        "no_matches": "Wiki search completed",
        "skipped": "Wiki search not needed",
        "failed": "Wiki search failed",
        "canceled": "Wiki search cancelled",
        "pending": "Wiki search interrupted",
    }
    assert label["textContent"] == expected[phase]
    meta, keywords_heading, chips, files_heading, files = content["children"]
    assert meta["children"][-1]["textContent"] == "Duration: 1.2s"
    if phase in {"ready", "fallback_ready", "no_matches"}:
        assert meta["children"][0]["textContent"] == "Matched 1 files"
    assert keywords_heading["textContent"] == "检索关键词"
    assert chips["children"][0]["textContent"] == "<img src=x onerror=alert(1)>"
    assert files_heading["textContent"] == "命中文档"
    assert files["children"][0]["children"][0]["textContent"] == "<script>untrusted</script>"
    assert files["children"][0]["children"][1]["textContent"] == "clouds/aliyun/ecs.md"
    number, excerpt = files["children"][0]["children"][2]["children"][0]["children"]
    assert number["textContent"] == "42"
    assert excerpt["children"][0]["tag"] == "strong"
    assert excerpt["children"][0]["textContent"] == "ECS"
    assert excerpt["children"][1]["textContent"] == " <img src=x onerror=alert(1)> [link](https://invalid.example)"


@pytest.mark.parametrize("scenario", ["before_model", "streaming", "completed", "history", "two_turns"])
def test_wiki_is_left_aligned_assistant_process_before_thinking(tmp_path, scenario):
    source = (Path(__file__).parents[2] / "src/iac_code/web/static/js/app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        "function " + name + "(" + source.split("function " + name + "(", 1)[1].split("\n}\n", 1)[0] + "\n}"
        for name in (
            "formatTurnDuration",
            "buildWikiSearchSummary",
            "renderWikiSearchProcess",
            "renderConversationMessage",
            "renderCollapsedTurn",
            "renderMessages",
        )
    )
    result = _run_events_script(
        tmp_path,
        """
        class Element {
          constructor(tag) {
            this.tag = tag; this.className = ''; this.children = []; this.dataset = {}; this.attributes = {};
            this.classList = {add: name => {this.className += ' ' + name;}};
          }
          append(...children) {this.children.push(...children);}
          replaceChildren(...children) {this.children = children;}
          setAttribute(key, value) {this.attributes[key] = value;}
          get childElementCount() {return this.children.length;}
        }
        const document = {createElement: tag => new Element(tag)};
        const stack = new Element('section');
        const byShell = () => stack;
        const text = value => String(value || '');
        const t = key => key;
        const noop = () => {};
        const applySpinPhase = noop, ensureMessageStackToggleSync = noop, setElementClassFlag = noop,
          restoreMessageStackSelection = noop, syncTurnThinking = noop, applyDetailsOpenOverrides = noop,
          scheduleMessageStackOverflowSync = noop;
        const captureMessageStackSelection = () => null;
        const isMessageStackNearBottom = () => true;
        const regroupPipelineMessages = messages => messages;
        const latestToolUseIdForTranscript = () => '';
        const buildMessageAttachmentsElement = () => null;
        const buildToolCardsElement = () => null;
        let pendingScrollToBottom = false, lastRenderPostHandoffNormal = false;
        function buildThinkingElement(message) {
          if (!message.thinking) return null;
          const element = new Element('details'); element.className = 'message-thinking'; return element;
        }
        function buildMessageBodyElement(message) {
          const element = new Element('div'); element.textContent = message.text; return element;
        }
        __FUNCTIONS__
        const scenario = __SCENARIO__;
        const active = ['before_model', 'streaming'].includes(scenario);
        const messages = {
          user1: {role:'user', messageId:'user1', turnId:'t1', sequence:1, text:'Question 1',
            wikiSearch:{queryId:'q1', phase:scenario === 'before_model' ? 'extracting' : 'ready',
              summary:'Wiki search', elapsedMs:1100, keywords:['ECS'], files:[]}}
        };
        if (scenario !== 'before_model') {
          messages.answer1 = {role:'assistant', messageId:'answer1', turnId:'t1', sequence:2,
            thinking:'reasoning', text:'Answer 1', status:active ? 'streaming' : 'completed', stored:!active};
        }
        if (scenario === 'two_turns') {
          messages.user2 = {role:'user', messageId:'user2', turnId:'t2', sequence:3, text:'Question 2'};
          messages.answer2 = {role:'assistant', messageId:'answer2', turnId:'t2', sequence:4,
            thinking:'other reasoning', text:'Answer 2', status:'completed'};
        }
        if (scenario === 'history') {
          messages.user1.turnId = ''; messages.user1.stored = true;
          messages.answer1.turnId = ''; messages.answer1.elapsedSeconds = 35;
        }
        renderMessages({messages, currentTurnActive:active, turns:{t1:{elapsedMs:35000}}});
        console.log(JSON.stringify(stack));
        """.replace("__FUNCTIONS__", functions).replace("__SCENARIO__", json.dumps(scenario)),
    )

    def descendants(node, class_name):
        found = [node] if class_name in node["className"].split() else []
        return found + [hit for child in node["children"] for hit in descendants(child, class_name)]

    assert not descendants(result["children"][0], "wiki-search-summary")
    assert result["children"][0]["className"] == "message message-user"
    assistant = result["children"][1]
    assert "message-agent" in assistant["className"]
    assert len(descendants(result, "wiki-search-summary")) == 1
    if scenario in {"before_model", "streaming"}:
        assert assistant["children"][0]["className"] == "wiki-search-summary"
        if scenario == "streaming":
            assert descendants(result["children"][2], "message-thinking")
    else:
        process = descendants(assistant, "turn-process-body")[0]
        assert process["children"][0]["className"] == "wiki-search-summary"
        assert process["children"][1]["className"] == "message-thinking"
    if scenario == "two_turns":
        assert not descendants(result["children"][3], "wiki-search-summary")


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
