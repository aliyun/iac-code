from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "selling_console_web" / "app.js"
STYLES_CSS = APP_JS.parent / "styles.css"
NODE_RELATIVE_PATH = Path(".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")


def bundled_node_candidates() -> list[Path]:
    override = os.environ.get("IAC_CODE_TEST_NODE")
    if override:
        return [Path(override).expanduser()]
    candidates = [Path.home() / NODE_RELATIVE_PATH]
    home_env = os.environ.get("HOME")
    if home_env:
        candidates.append(Path(home_env).expanduser() / NODE_RELATIVE_PATH)
    candidates.extend(parent / NODE_RELATIVE_PATH for parent in APP_JS.parents)
    return candidates


def node_command() -> list[str]:
    node = shutil.which("node")
    if node:
        return [node]
    for fallback in bundled_node_candidates():
        if fallback.exists():
            return [str(fallback)]
    pytest.skip("node is not installed")


def run_node_script(source: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="iac-code-selling-console-test-") as temp_dir:
        script_path = Path(temp_dir) / "script.js"
        script_path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [*node_command(), str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_run_node_script_uses_file_instead_of_inline_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    source = 'console.log(JSON.stringify({"ok": true}));'
    command_seen: list[str] = []

    def fake_run(command, *, capture_output, text, check, encoding):
        command_seen.extend(str(part) for part in command)
        assert capture_output is True
        assert text is True
        assert check is False
        assert encoding == "utf-8"
        assert "-e" not in command_seen
        script_path = Path(command_seen[-1])
        assert script_path.read_text(encoding="utf-8") == source
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_node_script(source) == {"ok": True}
    assert command_seen[:1] == ["/usr/bin/node"]


def test_node_command_falls_back_to_home_bundled_node_when_path_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_node = tmp_path / NODE_RELATIVE_PATH
    fake_node.parent.mkdir(parents=True)
    fake_node.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_node.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("IAC_CODE_TEST_NODE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert shutil.which("node") is None

    command = node_command()

    assert command == [str(fake_node)]
    assert Path(command[0]).exists()


def test_node_command_uses_env_override_when_path_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_node.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("IAC_CODE_TEST_NODE", str(fake_node))

    command = node_command()

    assert command == [str(fake_node)]


def reducer_harness(expression: str) -> dict:
    app_source = APP_JS.read_text(encoding="utf-8")
    script = f"""
const assert = require("assert");
global.window = {{}};
global.document = {{
  readyState: "loading",
  addEventListener() {{}},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
  getElementById() {{ return null; }}
}};
{app_source}
const reducers = window.SellingConsoleReducers;
const output = (() => {{
  {expression}
}})();
console.log(JSON.stringify(output));
"""
    return run_node_script(script)


def controller_harness(expression: str) -> dict:
    app_source = APP_JS.read_text(encoding="utf-8")
    script = f"""
class FakeElement {{
  constructor(tagName, id = "") {{
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = {{}};
    this.listeners = {{}};
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.clientHeight = 30;
  }}
  appendChild(child) {{
    this.children.push(child);
    return child;
  }}
  replaceChildren(...children) {{
    this.children = children;
    this.textContent = "";
  }}
  setAttribute(name, value) {{
    this.attributes[name] = String(value);
  }}
  getAttribute(name) {{
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }}
  addEventListener(name, listener) {{
    this.listeners[name] = this.listeners[name] || [];
    this.listeners[name].push(listener);
  }}
  click() {{
    (this.listeners.click || []).forEach((listener) => listener({{type: "click"}}));
  }}
}}
function walk(element, callback) {{
  if (!element) {{
    return;
  }}
  callback(element);
  (element.children || []).forEach((child) => walk(child, callback));
}}
function textOf(element) {{
  if (!element) {{
    return "";
  }}
  return [element.textContent || "", ...(element.children || []).map(textOf)].join("");
}}
const elements = {{
  "step-list": new FakeElement("div", "step-list"),
  "composer-progress": new FakeElement("div", "composer-progress"),
  "debug-drawer": new FakeElement("details", "debug-drawer"),
  "progress-debug-panel": new FakeElement("div", "progress-debug-panel"),
  "debug-output": new FakeElement("pre", "debug-output"),
  "debug-session-info": new FakeElement("div", "debug-session-info"),
  "normal-handoff-notice": new FakeElement("div", "normal-handoff-notice"),
  "plans-grid": new FakeElement("div", "plans-grid"),
  "status-pill": new FakeElement("span", "status-pill"),
  "status-alert": new FakeElement("div", "status-alert"),
  "server-url": new FakeElement("input", "server-url"),
  cwd: new FakeElement("input", "cwd"),
  "iac-code-model": new FakeElement("input", "iac-code-model"),
  "composer-input": new FakeElement("textarea", "composer-input"),
  "send-button": new FakeElement("button", "send-button"),
  "health-button": new FakeElement("button", "health-button"),
  "fetch-state-button": new FakeElement("button", "fetch-state-button"),
  "cancel-button": new FakeElement("button", "cancel-button"),
}};
elements["normal-handoff-notice"].hidden = true;
const debugPre = elements["debug-output"];
const roots = Object.values(elements);
global.window = {{SELLING_CONSOLE_DEFAULTS: {{serverUrl: "http://127.0.0.1:41299", cwd: "/workspace"}}}};
global.document = {{
  readyState: "loading",
  addEventListener() {{}},
  createElement(tagName) {{ return new FakeElement(tagName); }},
  getElementById(id) {{ return elements[id] || null; }},
  querySelector(selector) {{
    if (selector === "#debug-drawer pre") {{
      return debugPre;
    }}
    if (selector.startsWith("#")) {{
      return elements[selector.slice(1)] || null;
    }}
    return null;
  }},
  querySelectorAll(selector) {{
    const matches = [];
    roots.forEach((root) => walk(root, (element) => {{
      if (selector === "[data-step-id]" && element.getAttribute("data-step-id") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-event-kind]" && element.getAttribute("data-step-event-kind") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-state-icon]" && element.getAttribute("data-step-state-icon") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-toggle]" && element.getAttribute("data-step-toggle") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-result-field]" && element.getAttribute("data-step-result-field") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-result-option]" && element.getAttribute("data-step-result-option") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-candidate-result]" && element.getAttribute("data-step-candidate-result") !== null) {{
        matches.push(element);
      }}
      if (
        selector === "[data-step-candidate-result-summary]" &&
        element.getAttribute("data-step-candidate-result-summary") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-step-candidate-result-process]" &&
        element.getAttribute("data-step-candidate-result-process") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-step-candidate-progress]" &&
        element.getAttribute("data-step-candidate-progress") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-step-candidate-progress-head]" &&
        element.getAttribute("data-step-candidate-progress-head") !== null
      ) {{
        matches.push(element);
      }}
      if (selector === "[data-pending-input-kind]" && element.getAttribute("data-pending-input-kind") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-pending-input-option]" && element.getAttribute("data-pending-input-option") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-progress-step]" && element.getAttribute("data-progress-step") !== null) {{
        matches.push(element);
      }}
      if (
        selector === "[data-progress-variant-option]" &&
        element.getAttribute("data-progress-variant-option") !== null
      ) {{
        matches.push(element);
      }}
      if (selector === "[data-progress-param]" && element.getAttribute("data-progress-param") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-progress-param-group]" && element.getAttribute("data-progress-param-group") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-progress-step-option]" && element.getAttribute("data-progress-step-option") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-candidate-choice]" && element.getAttribute("data-candidate-choice") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-candidate-index]" && element.getAttribute("data-candidate-index") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-candidate-status]" && element.getAttribute("data-candidate-status") !== null) {{
        matches.push(element);
      }}
      if (
        selector === "[data-candidate-subpipeline]" &&
        element.getAttribute("data-candidate-subpipeline") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-candidate-subpipeline-body]" &&
        element.getAttribute("data-candidate-subpipeline-body") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-candidate-subpipeline-event]" &&
        element.getAttribute("data-candidate-subpipeline-event") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-candidate-subpipeline-toggle]" &&
        element.getAttribute("data-candidate-subpipeline-toggle") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-candidate-substep]" &&
        element.getAttribute("data-candidate-substep") !== null
      ) {{
        matches.push(element);
      }}
      if (selector === "[data-step-process]" && element.getAttribute("data-step-process") !== null) {{
        matches.push(element);
      }}
      if (selector === "[data-step-event-list]" && element.getAttribute("data-step-event-list") !== null) {{
        matches.push(element);
      }}
      if (
        selector === "[data-step-process-event]" &&
        element.getAttribute("data-step-process-event") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-debug-session-field]" &&
        element.getAttribute("data-debug-session-field") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-normal-handoff-message]" &&
        element.getAttribute("data-normal-handoff-message") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-chat-message]" &&
        element.getAttribute("data-chat-message") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-chat-avatar]" &&
        element.getAttribute("data-chat-avatar") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-normal-turn]" &&
        element.getAttribute("data-normal-turn") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-normal-process]" &&
        element.getAttribute("data-normal-process") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-normal-process-event]" &&
        element.getAttribute("data-normal-process-event") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-normal-answer]" &&
        element.getAttribute("data-normal-answer") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-markdown-node]" &&
        element.getAttribute("data-markdown-node") !== null
      ) {{
        matches.push(element);
      }}
      if (
        selector === "[data-template-popover]" &&
        element.getAttribute("data-template-popover") !== null
      ) {{
        matches.push(element);
      }}
    }}));
    return matches;
  }},
}};
{app_source}
(async () => {{
  const output = await (async () => {{
    const controller = window.SellingConsoleController;
    const debug = window.SellingConsoleDebug;
    const reducers = window.SellingConsoleReducers;
    const elementById = (id) => elements[id];
    const all = (selector) => document.querySelectorAll(selector);
    const text = textOf;
    const debugText = () => debugPre.textContent;
    {expression}
  }})();
  console.log(JSON.stringify(output));
}})().catch((error) => {{
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}});
"""
    return run_node_script(script)


def test_reducer_maps_pipeline_steps_to_console_sections() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({serverUrl: "http://127.0.0.1:41299", cwd: "/workspace"});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    taskId: "task-1",
    contextId: "ctx-1",
    sequence: 3,
    step: {id: "architecture_planning", name: "架构规划", status: "completed"}
  }}}
});
return {
  taskId: next.pipelineTaskId,
  contextId: next.contextId,
  sequence: next.lastSequence,
  architectureStatus: next.steps.architecture_planning.status
};
"""
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "sequence": 3,
        "architectureStatus": "completed",
    }


def test_reducer_uses_event_type_to_mark_completed_step_when_envelope_status_is_working() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "intent_parsing"},
    data: {
      conclusion: {
        scenario: "Nginx 静态站点",
        region: "华东 1（杭州）",
        budget: "低成本"
      }
    }
  }}}
});
return {
  status: next.steps.intent_parsing.status,
  eventCount: next.steps.intent_parsing.events.length
};
"""
    )

    assert output == {
        "status": "completed",
        "eventCount": 1,
    }


def test_reducer_keeps_parent_step_working_when_candidate_sub_step_completes() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.steps.evaluate_candidates.status = "working";
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimating"},
    data: {summary: "候选方案费用已估算"}
  }}}
});
return {
  status: next.steps.evaluate_candidates.status,
  eventCount: next.steps.evaluate_candidates.events.length
};
"""
    )

    assert output == {
        "status": "working",
        "eventCount": 1,
    }


def test_reducer_collects_candidate_details_from_tool_display() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState();
const next = reducers.reducePipelinePayload(state, {
  snapshot: {
    status: "waiting_input",
    display: {
      candidateDetails: [{
        candidateName: "ECS 经典网络方案",
        candidateIndex: 0,
        summary: "VPC + ECS + EIP",
        totalMonthlyCost: "¥33.89/月",
        costItems: [{name: "ECS", spec: "1vCPU/1GiB", monthly_cost: "¥33.89/月"}]
      }]
    },
    pendingInput: {
      kind: "ask_user_question",
      prompt: "请选择方案",
      options: [{id: "0", label: "ECS 经典网络方案"}]
    }
  }
});
return {
  candidateCount: next.candidates.length,
  candidateName: next.candidates[0].name,
  candidateCost: next.candidates[0].totalMonthlyCost,
  pendingPrompt: next.pendingInput.prompt
};
"""
    )

    assert output == {
        "candidateCount": 1,
        "candidateName": "ECS 经典网络方案",
        "candidateCost": "¥33.89/月",
        "pendingPrompt": "请选择方案",
    }


def test_reducer_preserves_zero_candidate_total_monthly_cost() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  snapshot: {
    display: {
      candidateDetails: [{
        candidateName: "免费方案",
        candidateIndex: 0,
        totalMonthlyCost: 0
      }]
    }
  }
});
return {
  totalMonthlyCost: next.candidates[0].totalMonthlyCost
};
"""
    )

    assert output == {"totalMonthlyCost": 0}


def test_reducer_collects_candidate_details_from_detail_wrapper() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  snapshot: {
    status: "waiting_input",
    display: {
      candidateDetails: [{
        detailId: "detail-1",
        candidate: {index: 0},
        step: {id: "confirm_and_select"},
        detail: {
          candidateName: "低成本 ECS 方案",
          summary: "single ecs",
          totalMonthlyCost: "CNY 60",
          costItems: [{name: "ecs", monthly_cost: "CNY 60"}]
        }
      }]
    }
  }
});
return {
  candidateCount: next.candidates.length,
  firstName: next.candidates[0].name,
  firstIndex: next.candidates[0].candidateIndex,
  firstSummary: next.candidates[0].summary,
  firstCost: next.candidates[0].totalMonthlyCost,
  firstCostItemName: next.candidates[0].costItems[0].name
};
"""
    )

    assert output == {
        "candidateCount": 1,
        "firstName": "低成本 ECS 方案",
        "firstIndex": 0,
        "firstSummary": "single ecs",
        "firstCost": "CNY 60",
        "firstCostItemName": "ecs",
    }


def test_reducer_collects_candidate_options_from_complete_step_conclusion() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "complete_step", status: "completed"},
    data: {
      conclusion: {
        options: [{
          title: "轻量应用服务器一体化方案",
          index: 1,
          summary: "开箱即用，管理简单。",
          totalMonthlyCost: "¥0/月"
        }]
      }
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0] && next.candidates[0].name,
  index: next.candidates[0] && next.candidates[0].candidateIndex,
  cost: next.candidates[0] && next.candidates[0].totalMonthlyCost
};
"""
    )

    assert output == {
        "count": 1,
        "name": "轻量应用服务器一体化方案",
        "index": 1,
        "cost": "¥0/月",
    }


def test_reducer_populates_candidate_summary_and_price_from_nested_candidate_payload() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "architecture_planning", status: "completed"},
    data: {
      conclusion: {
        candidates: [{
          index: 0,
          template: "创建基础 VPC 专有网络",
          candidate: {
            output_path: "templates/1-basic-vpc.yml",
            pros: "满足基础网络隔离需求、零成本、可按需扩展子网和安全组",
            cons: "仅含 VPC，需后续手动添加 VSwitch",
            monthly_estimate: 0
          },
          cost: {
            monthly_estimate: "¥0/月",
            currency: "CNY"
          }
        }]
      }
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0].name,
  summary: next.candidates[0].summary,
  totalMonthlyCost: next.candidates[0].totalMonthlyCost,
  outputPath: next.candidates[0].outputPath
};
"""
    )

    assert output == {
        "count": 1,
        "name": "创建基础 VPC 专有网络",
        "summary": "满足基础网络隔离需求、零成本、可按需扩展子网和安全组",
        "totalMonthlyCost": "¥0/月",
        "outputPath": "templates/1-basic-vpc.yml",
    }


def test_reducer_collects_step_two_draft_candidates_from_architecture_completion() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "architecture_planning", status: "completed"},
    data: {
      conclusion: {
        draft_candidates: [{
          candidate_index: 0,
          candidate_name: "基础 VPC 网络",
          first_version_description: "创建一个基础 VPC，作为后续云资源的网络容器。",
          rough_monthly_estimate: "¥0/月"
        }]
      }
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0] && next.candidates[0].name,
  summary: next.candidates[0] && next.candidates[0].summary,
  cost: next.candidates[0] && next.candidates[0].totalMonthlyCost
};
"""
    )

    assert output == {
        "count": 1,
        "name": "基础 VPC 网络",
        "summary": "创建一个基础 VPC，作为后续云资源的网络容器。",
        "cost": "¥0/月",
    }


def test_reducer_updates_candidate_summary_and_price_from_candidate_completed_event() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{candidateIndex: 0, name: "基础 VPC 网络"}];
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {
      candidate_name: "基础 VPC 网络",
      summary: "VPC 本身免费，适合作为后续子网和云资源的基础容器。",
      total_monthly_cost: "¥0/月"
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0].name,
  summary: next.candidates[0].summary,
  cost: next.candidates[0].totalMonthlyCost,
  subEventKind: next.candidates[0].subEvents[0].eventType
};
"""
    )

    assert output == {
        "count": 1,
        "name": "基础 VPC 网络",
        "summary": "VPC 本身免费，适合作为后续子网和云资源的基础容器。",
        "cost": "¥0/月",
        "subEventKind": "candidate_completed",
    }


def test_reducer_updates_candidate_from_nested_candidate_completed_conclusions() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{
  candidateIndex: 0,
  name: "经济型演示方案",
  summary: "成本最低，适合个人演示场景",
  totalMonthlyCost: "¥50 - ¥80"
}];
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0, name: "经济型演示方案"},
    data: {
      candidateIndex: 0,
      candidateName: "经济型演示方案",
      conclusions: {
        template: {
          file_path: "templates/1-economy-nginx.yml",
          description: "经济型 Nginx 演示环境 - VPC 内单可用区部署一台 ECS。"
        },
        cost: {
          monthly_estimate: "¥74/月",
          resources: [
            {type: "ECS 实例", cost: "¥34/月"},
            {type: "系统盘", cost: "¥40/月"}
          ]
        }
      }
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0].name,
  summary: next.candidates[0].summary,
  cost: next.candidates[0].totalMonthlyCost,
  outputPath: next.candidates[0].outputPath,
  costItemCount: next.candidates[0].costItems.length
};
"""
    )

    assert output == {
        "count": 1,
        "name": "经济型演示方案",
        "summary": "经济型 Nginx 演示环境 - VPC 内单可用区部署一台 ECS。",
        "cost": "¥74/月",
        "outputPath": "templates/1-economy-nginx.yml",
        "costItemCount": 2,
    }


def test_reducer_collects_snake_case_candidate_index_from_conclusion_options() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "complete_step", status: "completed"},
    data: {
      conclusion: {
        options: [{
          title: "低成本 ECS 方案",
          candidate_index: 3,
          total_monthly_cost: "¥33.89/月"
        }]
      }
    }
  }}}
});
reducers.selectCandidate(next, next.candidates[0].candidateIndex);
return {
  count: next.candidates.length,
  index: next.candidates[0].candidateIndex,
  cost: next.candidates[0].totalMonthlyCost,
  prompt: reducers.promptForSelectedCandidate(next)
};
"""
    )

    assert output == {
        "count": 1,
        "index": 3,
        "cost": "¥33.89/月",
        "prompt": "选择方案3",
    }


def test_reducer_does_not_mutate_original_state() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({
  serverUrl: "http://server",
  cwd: "/workspace",
  iacCodeModel: "kimi-k2.7-code"
});
const originalStep = state.steps.architecture_planning;
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    taskId: "task-1",
    contextId: "ctx-1",
    sequence: 1,
    step: {id: "architecture_planning", status: "completed"}
  }}}
});
return {
  sameState: next === state,
  sameSteps: next.steps === state.steps,
  sameStep: next.steps.architecture_planning === originalStep,
  originalTaskId: state.pipelineTaskId,
  originalStepStatus: state.steps.architecture_planning.status,
  originalEventCount: state.steps.architecture_planning.events.length,
  nextTaskId: next.pipelineTaskId,
  nextStepStatus: next.steps.architecture_planning.status,
  nextEventCount: next.steps.architecture_planning.events.length
};
"""
    )

    assert output == {
        "sameState": False,
        "sameSteps": False,
        "sameStep": False,
        "originalTaskId": "",
        "originalStepStatus": "pending",
        "originalEventCount": 0,
        "nextTaskId": "task-1",
        "nextStepStatus": "completed",
        "nextEventCount": 1,
    }


def test_reducer_collects_realtime_candidate_detail_event() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    taskId: "task-1",
    contextId: "ctx-1",
    sequence: 7,
    step: {id: "confirm_and_select", status: "working"},
    candidate: {index: 0},
    data: {
      detailId: "detail-1",
      detail: {
        candidateName: "低成本 ECS 方案",
        summary: "single ecs",
        totalMonthlyCost: "CNY 60",
        costItems: [{name: "ecs", monthly_cost: "CNY 60"}]
      }
    }
  }}}
});
return {
  count: next.candidates.length,
  name: next.candidates[0].name,
  index: next.candidates[0].candidateIndex,
  cost: next.candidates[0].totalMonthlyCost,
  eventCount: next.steps.confirm_and_select.events.length
};
"""
    )

    assert output == {
        "count": 1,
        "name": "低成本 ECS 方案",
        "index": 0,
        "cost": "CNY 60",
        "eventCount": 1,
    }


def test_reducer_does_not_retain_mutable_candidate_event_payload_references() -> None:
    output = reducer_harness(
        """
const costItems = [{name: "ecs"}];
const payload = {metadata: {iac_code: {pipeline: {
  eventType: "candidate_detail_shown",
  status: "working",
  taskId: "task-1",
  contextId: "ctx-1",
  sequence: 7,
  step: {id: "confirm_and_select", status: "working"},
  candidate: {index: 0},
  data: {
    detailId: "detail-1",
    detail: {
      candidateName: "低成本 ECS 方案",
      totalMonthlyCost: "CNY 60",
      costItems
    }
  }
}}}};
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), payload);
costItems[0].name = "mutated";
payload.metadata.iac_code.pipeline.data.detail.candidateName = "被污染";
return {
  eventName: next.steps.confirm_and_select.events[0].data.detail.candidateName,
  eventCostItemName: next.steps.confirm_and_select.events[0].data.detail.costItems[0].name,
  candidateName: next.candidates[0].name,
  candidateCostItemName: next.candidates[0].costItems[0].name
};
"""
    )

    assert output == {
        "eventName": "低成本 ECS 方案",
        "eventCostItemName": "ecs",
        "candidateName": "低成本 ECS 方案",
        "candidateCostItemName": "ecs",
    }


def test_reducer_clones_existing_step_events_when_cloning_state() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.steps.confirm_and_select.events.push({
  eventType: "candidate_detail_shown",
  data: {detail: {candidateName: "旧事件", costItems: [{name: "ecs"}]}}
});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "architecture_planning", status: "completed"}
  }}}
});
state.steps.confirm_and_select.events[0].data.detail.candidateName = "mutated";
state.steps.confirm_and_select.events[0].data.detail.costItems[0].name = "mutated";
return {
  sameEvent: next.steps.confirm_and_select.events[0] === state.steps.confirm_and_select.events[0],
  eventName: next.steps.confirm_and_select.events[0].data.detail.candidateName,
  costItemName: next.steps.confirm_and_select.events[0].data.detail.costItems[0].name
};
"""
    )

    assert output == {
        "sameEvent": False,
        "eventName": "旧事件",
        "costItemName": "ecs",
    }


def test_upsert_candidate_deep_clones_nested_payload() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const candidate = {
  name: "方案",
  candidateIndex: 0,
  metadata: {source: {tool: "planner"}},
  costItems: [{name: "ecs", detail: {region: "cn-hangzhou"}}]
};
const next = reducers.upsertCandidate(state, candidate);
candidate.name = "mutated";
candidate.metadata.source.tool = "mutated";
candidate.costItems[0].detail.region = "mutated";
return {
  name: next.candidates[0].name,
  tool: next.candidates[0].metadata.source.tool,
  region: next.candidates[0].costItems[0].detail.region
};
"""
    )

    assert output == {
        "name": "方案",
        "tool": "planner",
        "region": "cn-hangzhou",
    }


def test_reducer_events_only_payload_does_not_duplicate_first_event() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  events: [
    {eventType: "step_completed", sequence: 1, step: {id: "architecture_planning", status: "completed"}},
    {eventType: "step_completed", sequence: 2, step: {id: "evaluate_candidates", status: "completed"}}
  ]
});
return {
  architectureEvents: next.steps.architecture_planning.events.length,
  evaluateEvents: next.steps.evaluate_candidates.events.length,
  lastSequence: next.lastSequence
};
"""
    )

    assert output == {
        "architectureEvents": 1,
        "evaluateEvents": 1,
        "lastSequence": 2,
    }


def test_create_initial_state_does_not_alias_defaults_object() -> None:
    output = reducer_harness(
        """
const defaults = {serverUrl: "http://server", cwd: "/workspace", nested: {mode: "x"}};
const state = reducers.createInitialState(defaults);
defaults.serverUrl = "mutated";
defaults.nested.mode = "mutated";
return {
  serverUrl: state.serverUrl,
  defaultsServerUrl: state.defaults.serverUrl,
  defaultsMode: state.defaults.nested.mode
};
"""
    )

    assert output == {
        "serverUrl": "http://server",
        "defaultsServerUrl": "http://server",
        "defaultsMode": "x",
    }


def test_reducer_clones_existing_defaults_when_cloning_state() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({serverUrl: "http://server", cwd: "/workspace", nested: {mode: "x"}});
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "architecture_planning", status: "completed"}
  }}}
});
state.defaults.nested.mode = "mutated";
return {
  sameDefaults: next.defaults === state.defaults,
  nextMode: next.defaults.nested.mode
};
"""
    )

    assert output == {
        "sameDefaults": False,
        "nextMode": "x",
    }


def test_build_stream_payload_uses_active_task_before_handoff() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({
  serverUrl: "http://server",
  cwd: "/workspace",
  iacCodeModel: "kimi-k2.7-code"
});
state.contextId = "ctx-1";
state.pipelineTaskId = "pipeline-task";
state.activeTaskId = "active-task";
const beforeHandoff = reducers.buildStreamPayload(state, "部署 nginx");
state.normalHandoffReady = true;
const afterHandoff = reducers.buildStreamPayload(state, "继续部署");
return {
  beforeHandoff,
  afterHandoff
};
"""
    )

    assert output == {
        "beforeHandoff": {
            "serverUrl": "http://server",
            "cwd": "/workspace",
            "iacCodeModel": "kimi-k2.7-code",
            "contextId": "ctx-1",
            "taskId": "active-task",
            "prompt": "部署 nginx",
        },
        "afterHandoff": {
            "serverUrl": "http://server",
            "cwd": "/workspace",
            "iacCodeModel": "kimi-k2.7-code",
            "contextId": "ctx-1",
            "taskId": "",
            "prompt": "继续部署",
        },
    }


def test_candidate_selection_prompt_uses_zero_based_index() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [
  {name: "ECS 经典网络方案", candidateIndex: 0},
  {name: "轻量应用服务器一体化方案", candidateIndex: 1}
];
const selected = reducers.selectCandidate(state, 1);
return {
  sameState: selected === state,
  selected: state.selectedCandidateIndex,
  prompt: reducers.promptForSelectedCandidate(state),
  emptyPrompt: reducers.promptForSelectedCandidate(reducers.createInitialState({}))
};
"""
    )

    assert output == {
        "sameState": True,
        "selected": 1,
        "prompt": "选择方案1",
        "emptyPrompt": "",
    }


def test_controller_initially_hides_left_steps_and_composer_progress() -> None:
    output = controller_harness(
        """
controller.init();
const leftSteps = all("[data-step-id]");
const progressSteps = all("[data-progress-step]");
return {
  leftStepCount: leftSteps.length,
  progressCount: progressSteps.length,
  progressHidden: elementById("composer-progress").hidden,
  variant: elementById("composer-progress").getAttribute("data-progress-variant"),
  progressText: text(elementById("composer-progress"))
};
"""
    )

    assert output == {
        "leftStepCount": 0,
        "progressCount": 0,
        "progressHidden": True,
        "variant": "b",
        "progressText": "",
    }


def test_controller_reveals_composer_progress_after_pipeline_started() -> None:
    output = controller_harness(
        """
controller.init();
const initialHidden = elementById("composer-progress").hidden;
const next = reducers.reducePipelinePayload(debug.state(), {
  metadata: {iac_code: {pipeline: {
    eventType: "pipeline_started",
    status: "working",
    taskId: "task-1"
  }}}
});
Object.assign(debug.state(), next);
debug.render();
const progressSteps = all("[data-progress-step]");
return {
  initialHidden,
  progressHidden: elementById("composer-progress").hidden,
  mode: elementById("composer-progress").getAttribute("data-progress-mode"),
  progressCount: progressSteps.length,
  progressStatuses: progressSteps.map((step) => step.getAttribute("data-status")),
  progressText: text(elementById("composer-progress"))
};
"""
    )

    assert output == {
        "initialHidden": True,
        "progressHidden": False,
        "mode": "pipeline",
        "progressCount": 5,
        "progressStatuses": ["pending", "pending", "pending", "pending", "pending"],
        "progressText": "需求理解架构规划方案评估方案选择确认部署",
    }


def test_selling_console_chat_column_is_two_thirds_original_width() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(280px, 400px) minmax(0, 1fr) 56px;" in css
    assert "grid-template-columns: minmax(240px, 347px) minmax(0, 1fr);" in css


def test_selling_console_removes_left_ai_navigation_rail() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert 'class="ai-rail"' not in index_html
    assert "rail-bot" not in index_html
    assert "rail-button" not in index_html
    assert ".ai-rail" not in css


def test_selling_console_left_chat_scrolls_without_moving_composer() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    workflow_rule = css.split(".workflow-panel {", 1)[1].split("}", 1)[0]
    step_list_rule = css.split(".step-list {", 1)[1].split("}", 1)[0]
    completed_rule = css.split(".step-card.completed {", 1)[1].split("}", 1)[0]
    composer_rule = css.split(".composer {", 1)[1].split("}", 1)[0]

    assert "height: calc(100vh - 96px);" in workflow_rule
    assert "overflow: hidden;" in workflow_rule
    assert "align-content: start;" in step_list_rule
    assert "align-items: start;" in step_list_rule
    assert "overflow-y: auto;" in step_list_rule
    assert "min-height: 0;" in step_list_rule
    assert "flex: 1 1 auto;" in step_list_rule
    assert "gap: 5px;" in step_list_rule
    assert "padding: 8px 14px;" in step_list_rule
    assert "grid-template-columns: 24px 1fr;" in completed_rule
    assert "padding: 6px 8px;" in completed_rule
    assert "flex: 0 0 auto;" in composer_rule
    assert "border-top:" not in composer_rule


def test_selling_console_chat_messages_have_im_layout_and_avatars() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert ".chat-message.user" in css
    assert ".chat-message.system" in css
    assert ".chat-avatar.system" in css
    assert ".chat-avatar.user" in css


def test_selling_console_chat_and_progress_use_compact_spacing() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    chat_message_rule = css.split(".chat-message {", 1)[1].split("}", 1)[0]
    step_title_rule = css.split(".step-card h2 {", 1)[1].split("}", 1)[0]
    composer_rule = css.split(".composer {", 1)[1].split("}", 1)[0]
    composer_progress_rule = css.split(".composer-progress:not([hidden]) {", 1)[1].split("}", 1)[0]
    signal_circuit_rule = css.split(".signal-circuit {", 1)[1].split("}", 1)[0]
    signal_svg_rule = css.split(".signal-svg {", 1)[1].split("}", 1)[0]
    signal_labels_rule = css.split(".signal-labels {", 1)[1].split("}", 1)[0]

    assert "gap: 7px;" in chat_message_rule
    assert "font-size: 13px;" in step_title_rule
    assert "padding: 6px 14px 10px;" in composer_rule
    assert "margin-bottom: 8px;" in composer_progress_rule
    assert "padding-bottom: 8px;" in composer_progress_rule
    assert "height: 50px;" in signal_circuit_rule
    assert "height: 36px;" in signal_svg_rule
    assert "top: 32px;" in signal_labels_rule


def test_selling_console_step_rows_hide_sequence_numbers_and_use_compact_marker() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    step_index_rule = css.split(".step-index {", 1)[1].split("}", 1)[0]

    assert "step-number" not in APP_JS.read_text(encoding="utf-8")
    assert "width: 22px;" in step_index_rule
    assert "height: 22px;" in step_index_rule


def test_selling_console_left_intro_and_top_alert_are_visually_hidden() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    panel_heading_rule = css.split(".panel-heading {", 1)[1].split("}", 1)[0]
    status_alert_rule = css.split(".status-alert {", 1)[1].split("}", 1)[0]

    assert "display: none;" in panel_heading_rule
    assert "display: none;" in status_alert_rule


def test_selling_console_composer_uses_compact_input_box() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    composer_rule = css.split(".composer {", 1)[1].split("}", 1)[0]
    composer_box_rule = css.split(".composer-box {", 1)[1].split("}", 1)[0]
    input_rule = css.split("#composer-input {", 1)[1].split("}", 1)[0]
    send_button_rule = css.split(".send-icon-button {", 1)[1].split("}", 1)[0]
    mobile_compact_rule = css.split("@media (max-width: 560px)", 1)[1].split(".plan-meta", 1)[0]

    assert 'class="composer-box"' in index_html
    assert 'rows="2"' in index_html
    assert 'placeholder="继续补充您的需求，比如降低成本、提升可用性或约束地域"' in index_html
    assert 'aria-label="附件"' in index_html
    assert 'aria-label="发送"' in index_html
    assert "padding: 6px 14px 10px;" in composer_rule
    assert "padding: 10px 10px 9px;" in composer_box_rule
    assert "min-height: 40px;" in input_rule
    assert "border: 0;" in input_rule
    assert "resize: none;" in input_rule
    assert "width: 36px;" in send_button_rule
    assert "height: 36px;" in send_button_rule
    assert ".composer .send-icon-button" in mobile_compact_rule
    assert "width: 36px;" in mobile_compact_rule
    assert ".composer .icon-only-button" in mobile_compact_rule
    assert "width: 32px;" in mobile_compact_rule


def test_selling_console_connection_controls_live_in_debug_panel() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    plan_header = index_html.split('<div class="plan-header">', 1)[1].split('<div id="plans-grid"', 1)[0]
    debug_panel_intro = index_html.split('<div class="debug-panel">', 1)[1].split(
        '<div id="progress-debug-panel"',
        1,
    )[0]

    assert 'class="connection-controls"' not in plan_header
    assert 'class="connection-controls"' in debug_panel_intro


def test_selling_console_pipeline_diagnostics_output_is_collapsed_by_default() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    diagnostics = index_html.split('<details class="debug-output-block">', 1)[1].split("</details>", 1)[0]

    assert "<summary>Pipeline Diagnostics</summary>" in diagnostics
    assert '<details class="debug-output-block" open' not in index_html
    assert "<h2>Pipeline Diagnostics</h2>" not in index_html


def test_selling_console_has_handoff_notice_and_debug_session_info_slots() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")

    assert 'id="normal-handoff-notice"' not in index_html
    assert 'id="debug-session-info"' in index_html
    assert 'class="debug-session-info"' in index_html


def test_selling_console_candidate_subpipeline_body_is_height_limited() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert ".candidate-subpipeline-body" in css
    body_rule = css.split(".candidate-subpipeline-body", 1)[1].split("}", 1)[0]
    assert "max-height:" in body_rule
    assert "overflow-y: auto;" in body_rule


def test_selling_console_running_step_event_list_is_height_limited() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    event_list_rule = css.split(".step-event-list {", 1)[1].split("}", 1)[0]

    assert "max-height:" in event_list_rule
    assert "overflow-y: auto;" in event_list_rule


def test_selling_console_template_popover_can_be_entered_and_scrolled() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    popover_rule = css.split(".template-popover {", 1)[1].split("}", 1)[0]
    popover_hover_rule = css.split(".template-popover:hover {", 1)[1].split("}", 1)[0]

    assert ".template-popover-host:hover .template-popover" in css
    assert ".template-popover:hover" in css
    assert "max-height:" in popover_rule
    assert "overflow-y: auto;" in popover_rule
    assert "pointer-events: auto;" in popover_rule
    assert "transition-delay: 0ms, 0ms, 140ms;" in popover_rule
    assert "transition-delay: 500ms, 500ms, 500ms;" in popover_hover_rule


def test_selling_console_plan_grid_keeps_cards_top_aligned_when_process_expands() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    plans_grid_rule = css.split(".plans-grid", 1)[1].split("}", 1)[0]

    assert "align-items: start;" in plans_grid_rule


def test_selling_console_composer_progress_uses_separator_instead_of_floating_panel() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert ".composer-progress[hidden]" in css
    assert ".composer-progress:not([hidden])" in css
    visible_rule = css.split(".composer-progress:not([hidden])", 1)[1].split("}", 1)[0]
    assert "border-bottom: 1px solid var(--line);" in visible_rule
    assert "border-top:" not in visible_rule
    assert "box-shadow:" not in visible_rule
    assert "background:" not in visible_rule


def test_selling_console_progress_variants_match_unframed_visual_requirements() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")
    chevron_root_rule = css.split(".composer-progress.chevrons {", 1)[1].split("}", 1)[0]
    chevron_step_rule = css.split(".chevrons .step {", 1)[1].split("}", 1)[0]
    signal_rule = css.split(".signal-circuit {", 1)[1].split("}", 1)[0]

    assert "height: 32px;" in chevron_root_rule
    assert "font-size: 10px;" in chevron_step_rule
    assert "padding: 0 10px 0 14px;" in chevron_step_rule
    assert "border:" not in signal_rule
    assert "background:" not in signal_rule


def test_selling_console_progress_debug_panel_declares_three_adjustable_variants() -> None:
    index_html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    assert '<details id="debug-drawer" class="debug-drawer">' in index_html
    assert '<details id="debug-drawer" class="debug-drawer" open' not in index_html
    assert 'id="progress-debug-panel"' in index_html
    assert 'id="debug-output"' in index_html

    output = controller_harness(
        """
controller.init();
const variantButtons = all("[data-progress-variant-option]").map((button) => ({
  variant: button.getAttribute("data-progress-variant-option"),
  pressed: button.getAttribute("aria-pressed"),
  text: text(button)
}));
const paramGroups = all("[data-progress-param-group]").map((group) => ({
  variant: group.getAttribute("data-progress-param-group"),
  hidden: group.hidden
}));
const params = all("[data-progress-param]").map((input) => ({
  name: input.getAttribute("data-progress-param"),
  variant: input.getAttribute("data-progress-param-variant"),
  value: input.value
}));
return {
  progressVariant: elementById("composer-progress").getAttribute("data-progress-variant"),
  variantButtons,
  paramGroups,
  params
};
"""
    )

    assert output["progressVariant"] == "b"
    assert output["variantButtons"] == [
        {"variant": "a", "pressed": "false", "text": "A 箭头轨道"},
        {"variant": "b", "pressed": "true", "text": "B 脉冲线路"},
        {"variant": "d", "pressed": "false", "text": "D 输入框融合"},
    ]
    assert output["paramGroups"] == [
        {"variant": "a", "hidden": True},
        {"variant": "b", "hidden": False},
        {"variant": "d", "hidden": True},
    ]
    assert {"variant": "a", "name": "sweepMs", "value": "1800"} in output["params"]
    assert {"variant": "b", "name": "xPercent", "value": "28"} in output["params"]
    assert {"variant": "b", "name": "yPercent", "value": "49"} in output["params"]
    assert {"variant": "b", "name": "t1", "value": "140"} in output["params"]
    assert {"variant": "b", "name": "t2", "value": "540"} in output["params"]
    assert {"variant": "b", "name": "maxAmplitude", "value": "9"} in output["params"]
    assert {"variant": "b", "name": "pauseTime", "value": "510"} in output["params"]
    assert {"variant": "d", "name": "t1", "value": "1800"} in output["params"]
    assert {"variant": "d", "name": "t2", "value": "300"} in output["params"]
    assert all(item["name"] not in {"shineWidth", "lineWidth", "sweepWidth"} for item in output["params"])


def test_selling_console_progress_debug_panel_switches_variant_and_updates_param() -> None:
    output = controller_harness(
        """
controller.init();
const optionD = all("[data-progress-variant-option]").find((button) =>
  button.getAttribute("data-progress-variant-option") === "d"
);
optionD.click();
const afterSwitch = {
  progressVariant: elementById("composer-progress").getAttribute("data-progress-variant"),
  groups: all("[data-progress-param-group]").map((group) => ({
    variant: group.getAttribute("data-progress-param-group"),
    hidden: group.hidden
  }))
};
const dT1 = all("[data-progress-param]").find((input) =>
  input.getAttribute("data-progress-param-variant") === "d" &&
  input.getAttribute("data-progress-param") === "t1"
);
dT1.value = "2200";
(dT1.listeners.input || []).forEach((listener) => listener({type: "input"}));
return {
  afterSwitch,
  progressVariant: elementById("composer-progress").getAttribute("data-progress-variant"),
  stateValue: debug.state().progressUi.d.t1,
  renderedValue: all("[data-progress-param]").find((input) =>
    input.getAttribute("data-progress-param-variant") === "d" &&
    input.getAttribute("data-progress-param") === "t1"
  ).value
};
"""
    )

    assert output == {
        "afterSwitch": {
            "progressVariant": "d",
            "groups": [
                {"variant": "a", "hidden": True},
                {"variant": "b", "hidden": True},
                {"variant": "d", "hidden": False},
            ],
        },
        "progressVariant": "d",
        "stateValue": 2200,
        "renderedValue": "2200",
    }


def test_selling_console_debug_step_is_isolated_from_pipeline_progress() -> None:
    output = controller_harness(
        """
controller.init();
const drawer = elementById("debug-drawer");
const progress = elementById("composer-progress");
const initial = {
  hidden: progress.hidden,
  stepCount: all("[data-progress-step]").length
};
drawer.open = true;
(drawer.listeners.toggle || []).forEach((listener) => listener({type: "toggle"}));
all("[data-progress-step-option]")[3].click();
const debugOpen = {
  hidden: progress.hidden,
  mode: progress.getAttribute("data-progress-mode"),
  activeIndex: progress.children[0].getAttribute("data-active-index"),
  debugStep: debug.state().progressUi.activeStepIndex
};
drawer.open = false;
(drawer.listeners.toggle || []).forEach((listener) => listener({type: "toggle"}));
const closed = {
  hidden: progress.hidden,
  stepCount: all("[data-progress-step]").length,
  debugStep: debug.state().progressUi.activeStepIndex
};
const next = reducers.reducePipelinePayload(debug.state(), {
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "intent_parsing"}
  }}}
});
Object.assign(debug.state(), next);
debug.render();
const pipeline = {
  hidden: progress.hidden,
  mode: progress.getAttribute("data-progress-mode"),
  activeIndex: progress.children[0].getAttribute("data-active-index"),
  debugStep: debug.state().progressUi.activeStepIndex
};
return {initial, debugOpen, closed, pipeline};
"""
    )

    assert output == {
        "initial": {"hidden": True, "stepCount": 0},
        "debugOpen": {"hidden": False, "mode": "debug", "activeIndex": "3", "debugStep": 3},
        "closed": {"hidden": True, "stepCount": 0, "debugStep": 3},
        "pipeline": {"hidden": False, "mode": "pipeline", "activeIndex": "0", "debugStep": 3},
    }


def test_selling_console_progress_variants_use_prototype_dom_classes() -> None:
    output = controller_harness(
        """
controller.init();
const drawer = elementById("debug-drawer");
drawer.open = true;
(drawer.listeners.toggle || []).forEach((listener) => listener({type: "toggle"}));
const progress = elementById("composer-progress");
const bClass = progress.children[0].getAttribute("class");
all("[data-progress-variant-option]")
  .find((button) => button.getAttribute("data-progress-variant-option") === "a")
  .click();
const aRootClass = progress.getAttribute("class");
const aStepClasses = progress.children.map((child) => child.getAttribute("class"));
all("[data-progress-variant-option]")
  .find((button) => button.getAttribute("data-progress-variant-option") === "d")
  .click();
const dClass = progress.children[0].getAttribute("class");
return {
  bClass,
  aRootClass,
  aStepClasses,
  dClass
};
"""
    )

    assert output["bClass"] == "signal-circuit"
    assert "chevrons" in output["aRootClass"]
    assert output["aStepClasses"][0].startswith("step ")
    assert output["dClass"] == "fusion-label"


def test_selling_console_progress_uses_pipeline_active_step_when_debug_step_is_unset() -> None:
    output = controller_harness(
        """
controller.init();
debug.loadDemoCandidates();
const progress = elementById("composer-progress");
return {
  activeIndex: progress.children[0].getAttribute("data-active-index"),
  uiActiveStepIndex: debug.state().progressUi.activeStepIndex
};
"""
    )

    assert output == {
        "activeIndex": "3",
        "uiActiveStepIndex": None,
    }


def test_controller_reveals_running_step_events_then_collapses_completed_conclusion() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "intent_parsing"},
    data: {summary: "开始理解需求"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "intent_parsing"},
    data: {text: "正在分析地域与预算"}
  }}}
});
const runningText = text(all("[data-step-id]")[0]);
const runningProgress = all("[data-progress-step]").map((step) => ({
  id: step.getAttribute("data-progress-step"),
  status: step.getAttribute("data-status")
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "intent_parsing"},
    data: {
      conclusion: {
        scenario: "Nginx 静态站点",
        region: "华东 1（杭州）",
        budget: "低成本"
      }
    }
  }}}
});
const completedText = text(all("[data-step-id]")[0]);
const resultFields = all("[data-step-result-field]").map((field) => ({
  field: field.getAttribute("data-step-result-field"),
  text: text(field)
}));
const completedEvents = all("[data-step-event-kind]").map((event) => event.getAttribute("data-step-event-kind"));
const stateIcons = all("[data-step-state-icon]").map((icon) => ({
  state: icon.getAttribute("data-step-state-icon"),
  text: text(icon)
}));
const toggles = all("[data-step-toggle]");
toggles[0].click();
const expandedText = text(all("[data-step-id]")[0]);
const expandedFields = all("[data-step-result-field]").map((field) => text(field));
toggles[0].click();
const recollapsedText = text(all("[data-step-id]")[0]);
const completedProgress = all("[data-progress-step]").map((step) => ({
  id: step.getAttribute("data-progress-step"),
  status: step.getAttribute("data-status")
}));
return {
  stepCount: all("[data-step-id]").length,
  runningText,
  runningProgress,
  completedText,
  resultFields,
  completedEvents,
  stateIcons,
  toggleCount: toggles.length,
  expandedText,
  expandedFields,
  recollapsedText,
  completedProgress
};
"""
    )

    assert output["stepCount"] == 1
    assert "需求理解" in output["runningText"]
    assert "思考中" in output["runningText"]
    assert "开始理解需求" in output["runningText"]
    assert "正在分析地域与预算" in output["runningText"]
    assert {"id": "intent_parsing", "status": "working"} in output["runningProgress"]
    assert output["completedText"] == "✓需求理解"
    assert "思考完成" not in output["completedText"]
    assert "场景：Nginx 静态站点" not in output["completedText"]
    assert "地域：华东 1（杭州）" not in output["completedText"]
    assert "预算：低成本" not in output["completedText"]
    assert "；" not in output["completedText"]
    assert output["resultFields"] == []
    assert output["completedEvents"] == []
    assert output["stateIcons"] == [{"state": "completed", "text": "✓"}]
    assert output["toggleCount"] == 1
    assert "场景：Nginx 静态站点" in output["expandedText"]
    assert "地域：华东 1（杭州）" in output["expandedText"]
    assert "预算：低成本" in output["expandedText"]
    assert output["expandedFields"] == ["场景：Nginx 静态站点", "地域：华东 1（杭州）", "预算：低成本"]
    assert "场景：Nginx 静态站点" not in output["recollapsedText"]
    assert "正在分析地域与预算" not in output["completedText"]
    assert {"id": "intent_parsing", "status": "completed"} in output["completedProgress"]


def test_controller_renders_chat_messages_with_user_and_system_avatars() -> None:
    output = controller_harness(
        """
controller.init();
debug.state().userMessages = [{id: "u-1", text: "创建一个 VPC"}];
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "intent_parsing"},
    data: {summary: "开始理解需求"}
  }}}
});
return {
  messages: all("[data-chat-message]").map((item) => ({
    role: item.getAttribute("data-chat-message"),
    text: text(item)
  })),
  avatars: all("[data-chat-avatar]").map((item) => ({
    role: item.getAttribute("data-chat-avatar"),
    text: text(item)
  }))
};
"""
    )

    assert output["messages"][0] == {"role": "user", "text": "U创建一个 VPC"}
    assert output["messages"][1]["role"] == "system"
    assert output["messages"][1]["text"].startswith("AI")
    assert "需求理解思考中" in output["messages"][1]["text"]
    assert output["avatars"][:2] == [{"role": "user", "text": "U"}, {"role": "system", "text": "AI"}]


def test_controller_places_user_messages_after_related_pipeline_context() -> None:
    output = controller_harness(
        """
controller.init();
global.fetch = async () => ({
  ok: true,
  status: 200,
  body: null,
  text: async () => ""
});

elementById("composer-input").value = "选择一个已有vpc，创建一个vswitch";
await controller.sendComposerMessage();

const state = debug.state();
state.pipelineStarted = true;
state.steps.intent_parsing.status = "completed";
state.steps.architecture_planning.status = "completed";
state.steps.evaluate_candidates.status = "completed";
state.steps.confirm_and_select.status = "waiting_input";
state.status = "waiting_input";
state.pendingInput = {
  kind: "candidate_selection",
  prompt: "请选择要部署的方案：",
  options: [{id: "0", label: "方案0"}]
};
debug.render();

elementById("composer-input").value = "选择方案0";
await controller.sendComposerMessage();

state.steps.confirm_and_select.status = "completed";
state.steps.deploying.status = "completed";
state.pendingInput = null;
state.normalHandoffReady = true;
state.status = "completed";
debug.render();

elementById("composer-input").value = "刚才创建了什么？";
await controller.sendComposerMessage();

const messages = all("[data-chat-message]").map((item) => ({
  role: item.getAttribute("data-chat-message"),
  text: text(item)
}));
const indexOf = (needle) => messages.findIndex((item) => item.text.includes(needle));
return {
  messages,
  firstUser: indexOf("选择一个已有vpc"),
  selectStep: indexOf("方案选择"),
  secondUser: indexOf("选择方案0"),
  handoff: indexOf("部署流程已完成"),
  thirdUser: indexOf("刚才创建了什么")
};
"""
    )

    assert output["firstUser"] >= 0
    assert output["selectStep"] >= 0
    assert output["secondUser"] > output["selectStep"]
    assert output["handoff"] >= 0
    assert output["thirdUser"] > output["handoff"]


def test_controller_clears_composer_as_soon_as_message_is_submitted() -> None:
    output = controller_harness(
        """
controller.init();
let valueSeenByFetch = null;
global.fetch = async () => {
  valueSeenByFetch = elementById("composer-input").value;
  return {
    ok: true,
    status: 200,
    body: null,
    text: async () => ""
  };
};
elementById("composer-input").value = "创建一个 VPC";
await controller.sendComposerMessage();
return {
  valueSeenByFetch,
  finalValue: elementById("composer-input").value,
  messages: all("[data-chat-message]").map((item) => text(item))
};
"""
    )

    assert output["valueSeenByFetch"] == ""
    assert output["finalValue"] == ""
    assert any("创建一个 VPC" in item for item in output["messages"])


def test_controller_scrolls_left_chat_to_bottom_after_step_updates() -> None:
    output = controller_harness(
        """
controller.init();
const stepList = elementById("step-list");
stepList.scrollTop = 0;
stepList.scrollHeight = 240;
stepList.clientHeight = 60;
const next = reducers.reducePipelinePayload(debug.state(), {
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "intent_parsing"},
    data: {text: "正在持续分析用户需求，内容已经超过可视区域"}
  }}}
});
Object.assign(debug.state(), next);
debug.render();
return {
  scrollTop: stepList.scrollTop,
  scrollHeight: stepList.scrollHeight
};
"""
    )

    assert output == {"scrollTop": 240, "scrollHeight": 240}


def test_controller_scrolls_active_step_event_list_to_bottom_after_updates() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(text) {
  const next = reducers.reducePipelinePayload(debug.state(), {
    metadata: {iac_code: {pipeline: {
      eventType: "text_delta",
      status: "working",
      step: {id: "intent_parsing"},
      data: {text}
    }}}
  });
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload("第一段分析");
applyPayload("第二段分析");
const eventList = all("[data-step-event-list]")[0];
return {
  exists: Boolean(eventList),
  scrollTop: eventList && eventList.scrollTop,
  scrollHeight: eventList && eventList.scrollHeight
};
"""
    )

    assert output == {"exists": True, "scrollTop": 100, "scrollHeight": 100}


def test_controller_renders_normal_chat_answer_with_collapsible_process_after_handoff() -> None:
    output = controller_harness(
        """
controller.init();
global.fetch = async () => ({
  ok: true,
  status: 200,
  body: null,
  text: async () => [
    {
      taskId: "normal-task",
      contextId: "ctx-1",
      status: {state: "TASK_STATE_WORKING"},
      metadata: {iac_code: {thinking: {type: "raw_thinking", text: "读取刚才部署结果"}}}
    },
    {
      taskId: "normal-task",
      contextId: "ctx-1",
      status: {state: "TASK_STATE_WORKING"},
      metadata: {
        iac_code: {
          tool: {
            status: "completed",
            toolUseId: "toolu-read",
            name: "read_file",
            result: {content: "读取部署摘要"}
          }
        }
      }
    },
    {
      taskId: "normal-task",
      contextId: "ctx-1",
      status: {state: "TASK_STATE_WORKING", message: {parts: [{text: "刚才创建了一个 VSwitch。"}]}}
    },
    {
      taskId: "normal-task",
      contextId: "ctx-1",
      status: {state: "TASK_STATE_INPUT_REQUIRED"}
    }
  ].map((item) => `data: ${JSON.stringify(item)}`).join("\\n\\n")
});
Object.assign(debug.state(), {
  contextId: "ctx-1",
  normalHandoffReady: true,
  status: "completed"
});
debug.render();
elementById("composer-input").value = "刚才创建了什么？";
await controller.sendComposerMessage();

const messages = all("[data-chat-message]").map((item) => ({
  role: item.getAttribute("data-chat-message"),
  text: text(item)
}));
const turns = all("[data-normal-turn]").map((item) => ({
  id: item.getAttribute("data-normal-turn"),
  text: text(item)
}));
const process = all("[data-normal-process]")[0];
const events = all("[data-normal-process-event]").map((item) => ({
  kind: item.getAttribute("data-normal-process-event"),
  text: text(item)
}));
return {
  messages,
  turns,
  processOpen: process && process.open === true,
  processText: process && text(process),
  events,
  answer: text(all("[data-normal-answer]")[0]),
  normalStatus: debug.state().normalTurns[0] && debug.state().normalTurns[0].status
};
"""
    )

    assert any(item["role"] == "user" and "刚才创建了什么？" in item["text"] for item in output["messages"])
    assert len(output["turns"]) == 1
    assert output["normalStatus"] == "completed"
    assert output["processOpen"] is False
    assert output["events"] == [
        {"kind": "thinking", "text": "思考读取刚才部署结果"},
        {"kind": "tool", "text": "工具read_file 完成 读取部署摘要"},
    ]
    assert output["processText"].startswith("思考过程")
    assert output["answer"] == "刚才创建了一个 VSwitch。"


def test_controller_renders_normal_chat_answer_from_task_history_after_handoff() -> None:
    output = controller_harness(
        """
controller.init();
global.fetch = async () => ({
  ok: true,
  status: 200,
  body: null,
  text: async () => [
    {
      jsonrpc: "2.0",
      result: {
        id: "normal-task",
        contextId: "ctx-1",
        status: {state: "TASK_STATE_INPUT_REQUIRED"},
        history: [
          {
            role: "user",
            parts: [{root: {kind: "text", text: "你刚才创建了啥"}}]
          },
          {
            role: "agent",
            parts: [
              {root: {kind: "text", text: "刚才在已有 VPC 中新建了一个 VSwitch。"}},
              {root: {kind: "text", text: "VSwitch ID 是 vsw-123。"}}
            ]
          }
        ]
      }
    }
  ].map((item) => `data: ${JSON.stringify(item)}`).join("\\n\\n")
});
Object.assign(debug.state(), {
  contextId: "ctx-1",
  normalHandoffReady: true,
  status: "completed"
});
debug.render();
elementById("composer-input").value = "你刚才创建了啥";
await controller.sendComposerMessage();

return {
  turns: all("[data-normal-turn]").length,
  answer: text(all("[data-normal-answer]")[0]),
  normalStatus: debug.state().normalTurns[0] && debug.state().normalTurns[0].status
};
"""
    )

    assert output == {
        "turns": 1,
        "answer": "刚才在已有 VPC 中新建了一个 VSwitch。VSwitch ID 是 vsw-123。",
        "normalStatus": "completed",
    }


def test_controller_expanded_step_shows_all_conclusion_fields() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "intent_parsing"},
    data: {
      conclusion: {
        core_requirements: "VPC",
        cloud_platform: "aliyun",
        user_message_summary: "创建一个 VPC",
        non_functional: "低成本",
        additional_notes: "使用默认 CIDR",
        business_type: "网络基础设施",
        region_preference: "cn-hangzhou",
        risk: "后续需补充交换机"
      }
    }
  }}}
});
all("[data-step-toggle]")[0].click();
return {
  fields: all("[data-step-result-field]").map((field) => ({
    key: field.getAttribute("data-step-result-field"),
    text: text(field)
  }))
};
"""
    )

    assert [field["key"] for field in output["fields"]] == [
        "core_requirements",
        "cloud_platform",
        "user_message_summary",
        "non_functional",
        "additional_notes",
        "business_type",
        "region_preference",
        "risk",
    ]
    assert "后续需补充交换机" in output["fields"][-1]["text"]


def test_controller_merges_contiguous_text_delta_events_into_typing_card() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "intent_parsing"},
    data: {summary: "开始理解需求"}
  }}}
});
["正在分析", "地域、预算", "和部署约束"].forEach((fragment) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "intent_parsing"},
    data: {text: fragment}
  }}}
}));
const cards = all("[data-step-event-kind]").map((item) => ({
  kind: item.getAttribute("data-step-event-kind"),
  text: text(item)
}));
return {cards};
"""
    )

    assert [card["kind"] for card in output["cards"]] == ["step_started", "text_delta"]
    assert "思考片段" in output["cards"][1]["text"]
    assert "正在分析地域、预算和部署约束" in output["cards"][1]["text"]


def test_controller_shows_distinct_icons_for_running_completed_and_waiting_steps() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "intent_parsing"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "architecture_planning"},
    data: {summary: "规划网络拓扑"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {kind: "ask_user_question", prompt: "请选择方案"}
  }}}
});
return {
  icons: all("[data-step-state-icon]").map((icon) => ({
    state: icon.getAttribute("data-step-state-icon"),
    text: text(icon)
  }))
};
"""
    )

    assert output["icons"] == [
        {"state": "completed", "text": "✓"},
        {"state": "working", "text": "…"},
        {"state": "waiting_input", "text": "?"},
    ]


def test_controller_renders_tool_events_as_structured_step_event_cards() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "deploying"},
    data: {summary: "准备部署"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "tool_result",
    status: "working",
    step: {id: "deploying"},
    data: {
      toolName: "CreateStack",
      toolUseId: "tool-1",
      result: {
        stackId: "stack-123",
        stackStatus: "CREATE_COMPLETE"
      }
    }
  }}}
});
const eventCards = all("[data-step-event-kind]").map((item) => ({
  kind: item.getAttribute("data-step-event-kind"),
  text: text(item)
}));
return {
  count: eventCards.length,
  eventCards
};
"""
    )

    assert output["count"] == 2
    assert output["eventCards"][1]["kind"] == "tool_result"
    assert "工具结果" in output["eventCards"][1]["text"]
    assert "CreateStack" in output["eventCards"][1]["text"]
    assert "Tool Use" not in output["eventCards"][1]["text"]
    assert "tool-1" not in output["eventCards"][1]["text"]
    assert "stack-123" in output["eventCards"][1]["text"]
    assert "CREATE_COMPLETE" in output["eventCards"][1]["text"]


def test_controller_renders_candidate_subpipeline_below_matching_plan_card() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {index: 0, name: "标准 VPC 网络"},
  {index: 1, name: "VPC 含可用区交换机"}
].forEach((candidate) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: candidate.index},
    data: {detail: {candidateName: candidate.name, candidateIndex: candidate.index}}
  }}}
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimation", label: "成本估算", status: "working"},
    data: {summary: "开始估算成本"}
  }}}
});
["查询规格", "与价格"].forEach((fragment) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimation", label: "成本估算", status: "working"},
    data: {text: fragment}
  }}}
}));
const pipelines = all("[data-candidate-subpipeline]").map((item) => ({
  candidate: item.getAttribute("data-candidate-subpipeline"),
  open: item.open === true,
  text: text(item)
}));
const events = all("[data-candidate-subpipeline-event]").map((item) => ({
  kind: item.getAttribute("data-candidate-subpipeline-event"),
  text: text(item)
}));
return {pipelines, events};
"""
    )

    assert len(output["pipelines"]) == 1
    assert output["pipelines"][0]["candidate"] == "0"
    assert output["pipelines"][0]["open"] is True
    assert "思考过程" in output["pipelines"][0]["text"]
    assert "成本估算" in output["pipelines"][0]["text"]
    assert "开始估算成本" in output["pipelines"][0]["text"]
    assert [event["kind"] for event in output["events"]] == ["candidate_step_started", "text_delta"]
    assert "查询规格与价格" in output["events"][1]["text"]


def test_controller_auto_collapses_completed_candidate_subpipeline_on_plan_card() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "标准 VPC", candidateIndex: 0, summary: "基础网络", totalMonthlyCost: "¥0/月"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimation", label: "成本估算", status: "working"},
    data: {summary: "开始估算成本"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimation", label: "成本估算", status: "completed"},
    data: {summary: "成本估算完成"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "completed",
    step: {id: "evaluate_candidates"},
    data: {conclusion: {summary: "方案评估完成"}}
  }}}
});
const pipeline = all("[data-candidate-subpipeline]")[0];
const eventKinds = all("[data-candidate-subpipeline-event]")
  .map((item) => item.getAttribute("data-candidate-subpipeline-event"));
return {
  open: pipeline.open === true,
  text: text(pipeline),
  eventKinds
};
"""
    )

    assert output["open"] is False
    assert "思考过程" in output["text"]
    assert "思考完成" not in output["text"]
    assert output["eventKinds"] == ["candidate_step_started", "candidate_step_completed"]


def test_controller_updates_plan_card_and_collapses_subpipeline_when_candidate_completes() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "completed",
    step: {id: "architecture_planning"},
    data: {
      conclusion: {
        draft_candidates: [{
          candidate_index: 0,
          candidate_name: "基础 VPC 网络",
          first_version_description: "创建一个基础 VPC，作为后续云资源的网络容器。",
          rough_monthly_estimate: "待估算"
        }]
      }
    }
  }}}
});
const before = text(all("[data-candidate-index]")[0]);
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimation", label: "成本估算", status: "working"},
    data: {summary: "开始估算成本"}
  }}}
});
let pipeline = all("[data-candidate-subpipeline]")[0];
const openWhileWorking = pipeline.open === true;
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {
      candidate_name: "基础 VPC 网络",
      summary: "VPC 本身免费，适合作为后续子网和云资源的基础容器。",
      total_monthly_cost: "¥0/月"
    }
  }}}
});
pipeline = all("[data-candidate-subpipeline]")[0];
return {
  before,
  after: text(all("[data-candidate-index]")[0]),
  openWhileWorking,
  openAfterCandidateDone: pipeline.open === true,
  substepTexts: all("[data-candidate-substep]").map((item) => text(item)),
  subEventKinds: all("[data-candidate-subpipeline-event]")
    .map((item) => item.getAttribute("data-candidate-subpipeline-event"))
};
"""
    )

    assert "创建一个基础 VPC" in output["before"]
    assert "待估算" in output["before"]
    assert output["openWhileWorking"] is True
    assert "VPC 本身免费" in output["after"]
    assert "¥0/月" in output["after"]
    assert output["openAfterCandidateDone"] is False
    assert not any("方案思考" in item for item in output["substepTexts"])
    assert output["subEventKinds"] == ["candidate_step_started"]


def test_controller_plan_card_marks_candidate_working_then_completed() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "completed",
    step: {id: "architecture_planning"},
    data: {
      conclusion: {
        candidates: [{
          name: "经济型演示方案",
          candidate_index: 0,
          topology: "VPC 内单可用区部署一台突发性能 ECS。",
          monthly_estimate: "¥50 - ¥80"
        }]
      }
    }
  }}}
});
const initialCardText = text(all("[data-candidate-index]")[0]);
const initialPriceCount = (initialCardText.match(/¥50 - ¥80/g) || []).length;
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0, name: "经济型演示方案"},
    candidateStep: {id: "template_generating"}
  }}}
});
const workingStatus = all("[data-candidate-status]")[0];
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0, name: "经济型演示方案"},
    data: {
      candidateIndex: 0,
      candidateName: "经济型演示方案",
      conclusions: {
        template: {description: "经济型 Nginx 演示环境 - VPC 内单可用区部署一台 ECS。"},
        cost: {monthly_estimate: "¥74/月"}
      }
    }
  }}}
});
const completedStatus = all("[data-candidate-status]")[0];
return {
  initialCardText,
  initialPriceCount,
  workingStatus: {
    value: workingStatus.getAttribute("data-candidate-status"),
    text: text(workingStatus)
  },
  completedStatus: {
    value: completedStatus.getAttribute("data-candidate-status"),
    text: text(completedStatus)
  },
  completedCardText: text(all("[data-candidate-index]")[0])
};
"""
    )

    assert "预估价格" in output["initialCardText"]
    assert output["initialPriceCount"] == 1
    assert output["workingStatus"] == {"value": "working", "text": "生成中"}
    assert output["completedStatus"] == {"value": "completed", "text": "已完成"}
    assert "经济型 Nginx 演示环境" in output["completedCardText"]
    assert "¥74/月" in output["completedCardText"]


def test_controller_groups_candidate_subpipeline_into_expandable_substeps() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "基础 VPC 网络", candidateIndex: 0, summary: "基础网络", totalMonthlyCost: "¥0/月"}}
  }}}
});
[
  {
    eventType: "candidate_step_started",
    id: "template_generating",
    label: "模板生成",
    status: "working",
    summary: "开始生成模板"
  },
  {
    eventType: "tool_result",
    id: "template_generating",
    label: "模板生成",
    status: "working",
    summary: "写入模板",
    toolName: "write_file"
  },
  {
    eventType: "candidate_step_completed",
    id: "template_generating",
    label: "模板生成",
    status: "completed",
    summary: "模板生成完成"
  },
  {
    eventType: "candidate_step_started",
    id: "cost_estimating",
    label: "成本估算",
    status: "working",
    summary: "开始估算成本"
  }
].forEach((item) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: item.eventType,
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: item.id, label: item.label, status: item.status},
    data: {summary: item.summary, toolName: item.toolName}
  }}}
}));
const pipeline = all("[data-candidate-subpipeline]")[0];
const substeps = all("[data-candidate-substep]").map((item) => ({
  id: item.getAttribute("data-candidate-substep"),
  open: item.open === true,
  text: text(item)
}));
const events = all("[data-candidate-subpipeline-event]")
  .map((item) => item.getAttribute("data-candidate-subpipeline-event"));
return {
  pipelineOpen: pipeline.open === true,
  pipelineClickListeners: (pipeline.listeners.click || []).length,
  substeps,
  events
};
"""
    )

    assert output["pipelineOpen"] is True
    assert output["pipelineClickListeners"] >= 1
    assert [item["id"] for item in output["substeps"]] == ["template_generating", "cost_estimating"]
    assert "模板生成" in output["substeps"][0]["text"]
    assert "成本估算" in output["substeps"][1]["text"]
    assert output["substeps"][1]["open"] is True
    assert output["events"] == [
        "candidate_step_started",
        "tool_result",
        "candidate_step_completed",
        "candidate_step_started",
    ]


def test_controller_marks_candidate_substeps_complete_after_evaluation_step_completes() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "基础 VPC 网络", candidateIndex: 0, summary: "基础网络", totalMonthlyCost: "¥0/月"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {summary: "开始生成模板"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {text: "模板内容已生成"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {conclusion: {summary: "方案评估完成"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {kind: "candidate_selection", prompt: "请选择购买方案", options: [{id: "0", label: "基础 VPC 网络"}]}
  }}}
});
const substeps = all("[data-candidate-substep]").map((item) => ({
  id: item.getAttribute("data-candidate-substep"),
  open: item.open === true,
  text: text(item)
}));
return {substeps};
"""
    )

    assert output["substeps"] == [
        {
            "id": "template_generating",
            "open": False,
            "text": "模板生成完成子步骤开始开始生成模板思考片段模板内容已生成",
        }
    ]


def test_controller_preserves_open_candidate_subpipeline_when_events_update() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "基础 VPC 网络", candidateIndex: 0, summary: "基础网络"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {summary: "开始生成模板"}
  }}}
});
let pipeline = all("[data-candidate-subpipeline]")[0];
pipeline.open = true;
(pipeline.listeners.toggle || []).forEach((listener) => listener({type: "toggle"}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {text: "继续生成"}
  }}}
});
pipeline = all("[data-candidate-subpipeline]")[0];
return {
  openAfterUpdate: pipeline.open === true,
  stored: debug.state().expandedCandidateSubpipelines["0"] === true
};
"""
    )

    assert output == {"openAfterUpdate": True, "stored": True}


def test_controller_auto_opens_and_scrolls_candidate_subpipeline_while_evaluating() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {summary: "开始评估方案"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "基础 VPC 网络", candidateIndex: 0, summary: "基础网络"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {summary: "开始生成模板"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "template_generating", status: "working"},
    data: {text: "继续生成"}
  }}}
});
const pipeline = all("[data-candidate-subpipeline]")[0];
const body = all("[data-candidate-subpipeline-body]")[0];
return {
  open: pipeline.open === true,
  scrollTop: body && body.scrollTop,
  scrollHeight: body && body.scrollHeight
};
"""
    )

    assert output == {"open": True, "scrollTop": 100, "scrollHeight": 100}


def test_controller_candidate_subpipeline_keeps_all_chinese_substeps_and_auto_opens_body() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {detail: {candidateName: "基础 VPC 网络", candidateIndex: 0, summary: "基础网络", totalMonthlyCost: "¥0/月"}}
  }}}
});
[
  "template_generating",
  "cost_estimating",
  "quality_review"
].forEach((stepId, stepIndex) => {
  for (let index = 0; index < 9; index += 1) {
    const isLast = index === 8;
    applyPayload({
      metadata: {iac_code: {pipeline: {
        eventType: isLast ? "candidate_step_completed" : index === 0 ? "candidate_step_started" : "text_delta",
        status: "working",
        step: {id: "evaluate_candidates"},
        candidate: {index: 0},
        candidateStep: {id: stepId, status: isLast ? "completed" : "working"},
        data: {text: `片段 ${stepIndex}-${index}`, summary: `子步骤 ${stepIndex}-${index}`}
      }}}
    });
  }
});
const pipeline = all("[data-candidate-subpipeline]")[0];
const toggle = all("[data-candidate-subpipeline-toggle]")[0];
const substeps = all("[data-candidate-substep]").map((item) => text(item));
return {
  pipelineOpen: pipeline.open === true,
  pipelineText: text(pipeline),
  toggleText: text(toggle),
  substeps
};
"""
    )

    assert output["pipelineOpen"] is True
    assert output["toggleText"] == "思考过程"
    assert "思考完成" not in output["pipelineText"]
    assert any("模板生成" in item for item in output["substeps"])
    assert any("成本估算" in item for item in output["substeps"])
    assert any("质量复核" in item for item in output["substeps"])
    assert not any("template_generating" in item or "cost_estimating" in item for item in output["substeps"])


def test_controller_collapses_step_three_completion_in_left_chat_without_duplicate_option_details() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {summary: "开始评估候选方案"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {
      conclusion: {
        options: [
          {
            title: "标准 VPC 网络",
            candidateIndex: 0,
            summary: "成本较低，扩展性一般",
            totalMonthlyCost: "¥33.89/月"
          },
          {
            title: "VPC 含可用区交换机",
            candidateIndex: 1,
            summary: "自动创建交换机，部署更顺滑",
            totalMonthlyCost: "¥60/月"
          }
        ]
      }
    }
  }}}
});
const stepText = text(all("[data-step-id]")[0]);
const resultOptions = all("[data-step-result-option]").map((item) => ({
  option: item.getAttribute("data-step-result-option"),
  text: text(item)
}));
return {stepText, resultOptions};
"""
    )

    assert "；" not in output["stepText"]
    assert "已生成 2 个方案" not in output["stepText"]
    assert "成本较低，扩展性一般" not in output["stepText"]
    assert "自动创建交换机，部署更顺滑" not in output["stepText"]
    assert output["resultOptions"] == []


def test_controller_renders_step_three_nested_candidate_conclusion_without_flat_object_text() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {
      conclusion: {
        0: {
          template: "基础 VPC 网络模板，创建 CIDR 192.168.0.0/16 的专有网络",
          cost: {
            template_fixed: false,
            monthly_estimate: "¥0/月",
            currency: "CNY",
            api_raw_summary: "GetTemplateEstimateCost 返回 Resources: {}，VPC 为免费资源"
          },
          candidate: {
            name: "基础 VPC 网络",
            output_path: "templates/1-basic-vpc.yml",
            pros: "满足基础网络隔离需求、零成本、可按需扩展子网和安全组",
            monthly_estimate: 0,
            cons: "仅含 VPC，需后续手动添加 VSwitch"
          }
        }
      }
    }
  }}}
});
all("[data-step-toggle]")[0].click();
const stepText = text(all("[data-step-id]")[0]);
const resultOptions = all("[data-step-result-option]").map((item) => text(item));
const candidateResults = all("[data-step-candidate-result]").map((item) => text(item));
const resultFields = all("[data-step-result-field]").map((field) => text(field));
return {stepText, resultOptions, candidateResults, resultFields};
"""
    )

    assert "cost：" not in output["stepText"]
    assert "candidate：" not in output["stepText"]
    assert "template fixed" not in output["stepText"]
    assert "；" not in output["stepText"]
    assert output["resultFields"] == []
    assert output["resultOptions"] == []
    assert len(output["candidateResults"]) == 1
    assert "基础 VPC 网络" in output["candidateResults"][0]
    assert "基础 VPC 网络模板" in output["candidateResults"][0]
    assert "¥0/月" in output["candidateResults"][0]


def test_controller_compacts_long_template_text_in_step_three_candidate_result() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
const fullTemplate = [
  "ROSTemplateFormatVersion: '2015-09-01'",
  "Description:",
  "  zh-cn: 经济型突发实例方案，使用 Nginx 托管静态网站",
  "Resources:",
  "  WebServer:",
  "    Type: ALIYUN::ECS::Instance",
  "    Properties:",
  "      InstanceType: ecs.t6-c1m1.large",
  "      SystemDiskCategory: cloud_essd",
  "      InternetMaxBandwidthOut: 1"
].join("\\n");
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    data: {
      detail: {
        candidateName: "经济型突发实例方案",
        candidateIndex: 0,
        template: fullTemplate,
        totalMonthlyCost: "¥24.51/月"
      }
    }
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "completed",
    step: {id: "evaluate_candidates"},
    data: {conclusion: {summary: "方案评估完成"}}
  }}}
});
all("[data-step-toggle]")[0].click();
const result = all("[data-step-candidate-result]")[0];
const planCard = all("[data-candidate-index]")[0];
const summary = all("[data-step-candidate-result-summary]")[0] || null;
const popovers = all("[data-template-popover]").map((popover) => text(popover));
return {
  resultText: text(result),
  resultTitle: result ? result.getAttribute("title") : null,
  planTitle: planCard ? planCard.getAttribute("title") : null,
  summaryText: summary ? text(summary) : "",
  summaryTitle: summary ? summary.getAttribute("title") : null,
  popovers
};
"""
    )

    assert "经济型突发实例方案" in output["resultText"]
    assert "¥24.51/月" in output["resultText"]
    assert "ROSTemplateFormatVersion" not in output["summaryText"]
    assert "Resources:" not in output["summaryText"]
    assert "模板内容已生成" in output["summaryText"]
    assert output["resultTitle"] is None
    assert output["planTitle"] is None
    assert output["summaryTitle"] is None
    assert len(output["popovers"]) == 2
    assert all("ROSTemplateFormatVersion" in item for item in output["popovers"])
    assert all("Resources:" in item for item in output["popovers"])


def test_controller_step_three_expansion_groups_summary_and_process_by_candidate() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {index: 0, name: "基础 VPC 网络", summary: "VPC 本身免费，适合先建立网络容器", price: "¥0/月"},
  {index: 1, name: "VPC 含交换机", summary: "自动创建交换机，后续部署更顺滑", price: "¥0/月"}
].forEach((candidate) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: candidate.index},
    data: {
      detail: {
        candidateName: candidate.name,
        candidateIndex: candidate.index,
        summary: candidate.summary,
        totalMonthlyCost: candidate.price
      }
    }
  }}}
}));
[
  {candidateIndex: 0, stepId: "template_generating", text: "生成 VPC 模板"},
  {candidateIndex: 0, stepId: "cost_estimating", text: "确认 VPC 免费"},
  {candidateIndex: 1, stepId: "template_generating", text: "生成 VPC 与交换机模板"},
  {candidateIndex: 1, stepId: "cost_estimating", text: "确认网络资源免费"}
].forEach((item) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "text_delta",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: item.candidateIndex},
    candidateStep: {id: item.stepId, status: "working"},
    data: {text: item.text}
  }}}
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    status: "working",
    step: {id: "evaluate_candidates"},
    data: {conclusion: {
      0: {candidate: {name: "基础 VPC 网络"}, summary: "VPC 本身免费，适合先建立网络容器"},
      1: {candidate: {name: "VPC 含交换机"}, summary: "自动创建交换机，后续部署更顺滑"}
    }}
  }}}
});
all("[data-step-toggle]")[0].click();
const results = all("[data-step-candidate-result]").map((item) => ({
  candidate: item.getAttribute("data-step-candidate-result"),
  text: text(item)
}));
const processes = all("[data-step-candidate-result-process]").map((item) => ({
  candidate: item.getAttribute("data-step-candidate-result-process"),
  open: item.open === true,
  text: text(item)
}));
return {results, processes};
"""
    )

    assert [item["candidate"] for item in output["results"]] == ["0", "1"]
    assert "基础 VPC 网络" in output["results"][0]["text"]
    assert "VPC 本身免费" in output["results"][0]["text"]
    assert "VPC 含交换机" in output["results"][1]["text"]
    assert "自动创建交换机" in output["results"][1]["text"]
    assert [item["candidate"] for item in output["processes"]] == ["0", "1"]
    assert output["processes"][0]["open"] is False
    assert "模板生成" in output["processes"][0]["text"]
    assert "成本估算" in output["processes"][0]["text"]
    assert "生成 VPC 模板" in output["processes"][0]["text"]
    assert "生成 VPC 与交换机模板" in output["processes"][1]["text"]


def test_controller_completed_step_expansion_includes_collapsible_process() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {eventType: "step_started", status: "working", data: {summary: "开始理解需求"}},
  {eventType: "text_delta", status: "working", data: {text: "识别 VPC"}},
  {eventType: "tool_result", status: "working", data: {toolName: "read_context", result: {status: "success"}}},
  {eventType: "step_completed", status: "working", data: {conclusion: {
    core_requirements: "VPC",
    cloud_platform: "aliyun",
    user_message_summary: "创建一个 VPC"
  }}}
].forEach((event) => applyPayload({
  metadata: {iac_code: {pipeline: {
    ...event,
    step: {id: "intent_parsing"}
  }}}
}));
all("[data-step-toggle]")[0].click();
const step = all("[data-step-id]")[0];
const process = all("[data-step-process]")[0];
const processEvents = all("[data-step-process-event]").map((item) => ({
  kind: item.getAttribute("data-step-process-event"),
  text: text(item)
}));
return {
  stepText: text(step),
  processOpen: process.open === true,
  processText: text(process),
  processEvents
};
"""
    )

    assert "VPC" in output["stepText"]
    assert "思考过程" in output["processText"]
    assert output["processOpen"] is False
    assert [event["kind"] for event in output["processEvents"]] == [
        "step_started",
        "text_delta",
        "tool_result",
        "step_completed",
    ]
    assert "识别 VPC" in output["processEvents"][1]["text"]


def test_controller_summarizes_step_three_left_chat_by_candidate_latest_progress() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {
    index: 0,
    name: "基础 VPC 网络",
    summary: "使用 192.168.0.0/16 网段，作为后续网络资源的基础容器。"
  },
  {
    index: 1,
    name: "VPC 含可用区交换机",
    summary: "创建 VPC 和可用区交换机，后续可直接部署 ECS。"
  }
].forEach((candidate) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: candidate.index},
    data: {detail: {
      candidateName: candidate.name,
      candidateIndex: candidate.index,
      summary: candidate.summary,
      totalMonthlyCost: "¥0/月"
    }}
  }}}
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimating", label: "成本估算"},
    data: {summary: "开始估算成本"}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "tool_result",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 0},
    candidateStep: {id: "cost_estimating", label: "成本估算"},
    data: {toolName: "GetTemplateEstimateCost", result: {status: "success"}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_step_started",
    status: "working",
    step: {id: "evaluate_candidates"},
    candidate: {index: 1},
    candidateStep: {id: "template_validating", label: "模板校验"},
    data: {summary: "校验模板参数"}
  }}}
});
const stepText = text(all("[data-step-id]")[0]);
const heads = all("[data-step-candidate-progress-head]").map((item) => text(item));
const summaries = all("[data-step-candidate-progress]").map((item) => ({
  index: item.getAttribute("data-step-candidate-progress"),
  text: text(item)
}));
return {stepText, heads, summaries};
"""
    )

    assert output["heads"] == ["方案 0基础 VPC 网络", "方案 1VPC 含可用区交换机"]
    assert len(output["summaries"]) == 2
    assert output["summaries"] == [
        {"index": "0", "text": "方案 0基础 VPC 网络工具结果GetTemplateEstimateCost"},
        {"index": "1", "text": "方案 1VPC 含可用区交换机模板校验校验模板参数"},
    ]
    assert "使用 192.168.0.0/16 网段" not in output["stepText"]
    assert "创建 VPC 和可用区交换机" not in output["stepText"]


def test_controller_renders_generic_pending_input_options_in_left_chat() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    input: {
      kind: "ask_user_question",
      inputId: "ask-1",
      question: "请选择部署目标",
      options: [
        {id: "nginx", label: "Nginx 网站", description: "托管静态站点"},
        {id: "api", label: "API 服务", description: "后端接口"}
      ]
    }
  }}}
});
const cards = all("[data-pending-input-kind]");
const options = all("[data-pending-input-option]").map((option) => ({
  id: option.getAttribute("data-pending-input-option"),
  text: text(option)
}));
all("[data-pending-input-option]")[1].click();
const optionsAfter = all("[data-pending-input-option]").map((option) => ({
  id: option.getAttribute("data-pending-input-option"),
  pressed: option.getAttribute("aria-pressed"),
  className: option.getAttribute("class")
}));
return {
  cardCount: cards.length,
  options,
  optionsAfter,
  pendingKind: debug.state().pendingInput.kind,
  pendingPrompt: debug.state().pendingInput.prompt,
  pendingOptionCount: debug.state().pendingInput.options.length,
  selectedPendingInputOptionId: debug.state().selectedPendingInputOptionId,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert output == {
        "cardCount": 1,
        "options": [
            {"id": "nginx", "text": "Nginx 网站托管静态站点"},
            {"id": "api", "text": "API 服务后端接口"},
        ],
        "optionsAfter": [
            {"id": "nginx", "pressed": "false", "className": "pending-input-option"},
            {"id": "api", "pressed": "true", "className": "pending-input-option selected"},
        ],
        "pendingKind": "ask_user_question",
        "pendingPrompt": "请选择部署目标",
        "pendingOptionCount": 2,
        "selectedPendingInputOptionId": "api",
        "composerValue": "api",
    }


def test_controller_renders_pending_input_markdown_for_questions_and_candidate_selection() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "intent_parsing"},
    input: {
      kind: "ask_user_question",
      question: "请选择 **部署目标**：\\n\\n- Nginx 网站\\n- API 服务\\n\\n查看 [帮助](https://example.com/docs)",
      options: [
        {id: "nginx", label: "Nginx 网站", description: "用于 **静态站点**"}
      ]
    }
  }}}
});
const askCard = all("[data-pending-input-kind]")[0];
const askMarkdown = all("[data-markdown-node]").map((node) => ({
  kind: node.getAttribute("data-markdown-node"),
  tag: node.tagName,
  text: text(node),
  href: node.getAttribute("href")
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    input: {
      kind: "candidate_select",
      question: "请选择要部署的方案：**方案 0** 或 **方案 1**",
      options: [
        {id: "0", label: "经济型方案", description: "适合 **低成本** 演示"}
      ]
    }
  }}}
});
const candidateCard = all("[data-pending-input-kind]")[0];
const candidateMarkdown = all("[data-markdown-node]").map((node) => ({
  kind: node.getAttribute("data-markdown-node"),
  tag: node.tagName,
  text: text(node),
  href: node.getAttribute("href")
}));
return {
  askText: text(askCard),
  askMarkdown,
  candidateText: text(candidateCard),
  candidateMarkdown
};
"""
    )

    assert "**部署目标**" not in output["askText"]
    assert "查看 帮助" in output["askText"]
    assert {"kind": "strong", "tag": "STRONG", "text": "部署目标", "href": None} in output["askMarkdown"]
    assert {"kind": "li", "tag": "LI", "text": "Nginx 网站", "href": None} in output["askMarkdown"]
    assert {"kind": "a", "tag": "A", "text": "帮助", "href": "https://example.com/docs"} in output["askMarkdown"]
    assert "**方案 0**" not in output["candidateText"]
    assert "方案 0" in output["candidateText"]
    assert {"kind": "strong", "tag": "STRONG", "text": "方案 0", "href": None} in output["candidateMarkdown"]
    assert {"kind": "strong", "tag": "STRONG", "text": "低成本", "href": None} in output["candidateMarkdown"]


def test_controller_renders_inline_numbered_pending_input_as_ordered_list() -> None:
    output = controller_harness(
        """
controller.init();
const next = reducers.reducePipelinePayload(debug.state(), {
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "intent_parsing"},
    input: {
      kind: "ask_user_question",
      question: "请补充以下信息： 1. 演示内容：**静态页面**、反向代理还是其他？ 2. 是否需要公网访问？ 3. 预算偏好？"
    }
  }}}
});
Object.assign(debug.state(), next);
debug.render();
const card = all("[data-pending-input-kind]")[0];
const markdown = all("[data-markdown-node]").map((node) => ({
  kind: node.getAttribute("data-markdown-node"),
  tag: node.tagName,
  text: text(node)
}));
return {cardText: text(card), markdown};
"""
    )

    assert "1. 演示内容" not in output["cardText"]
    assert {
        "kind": "ol",
        "tag": "OL",
        "text": "演示内容：静态页面、反向代理还是其他？是否需要公网访问？预算偏好？",
    } in output["markdown"]
    assert {"kind": "li", "tag": "LI", "text": "演示内容：静态页面、反向代理还是其他？"} in output["markdown"]
    assert {"kind": "li", "tag": "LI", "text": "是否需要公网访问？"} in output["markdown"]
    assert {"kind": "li", "tag": "LI", "text": "预算偏好？"} in output["markdown"]
    assert {"kind": "strong", "tag": "STRONG", "text": "静态页面"} in output["markdown"]


def test_controller_ask_user_question_candidate_option_syncs_with_right_plan_card() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {index: 0, name: "经济型演示方案", summary: "成本最低", price: "¥74/月"},
  {index: 1, name: "均衡型演示方案", summary: "性能稳定", price: "¥291/月"}
].forEach((candidate) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "confirm_and_select"},
    candidate: {index: candidate.index},
    data: {
      detail: {
        candidateName: candidate.name,
        candidateIndex: candidate.index,
        summary: candidate.summary,
        totalMonthlyCost: candidate.price
      }
    }
  }}}
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    input: {
      kind: "ask_user_question",
      question: "请选择要部署的方案",
      options: [
        {id: "use-economy", label: "选择经济型", candidate_index: 0},
        {id: "use-balanced", label: "选择均衡型", candidate_index: 1}
      ]
    }
  }}}
});
all("[data-pending-input-option]")
  .find((option) => option.getAttribute("data-pending-input-option") === "use-balanced")
  .click();
const leftOptions = all("[data-pending-input-option]").map((option) => ({
  id: option.getAttribute("data-pending-input-option"),
  candidateChoice: option.getAttribute("data-candidate-choice"),
  pressed: option.getAttribute("aria-pressed"),
  className: option.getAttribute("class")
}));
const rightCards = all("[data-candidate-index]").map((card) => ({
  index: card.getAttribute("data-candidate-index"),
  pressed: card.getAttribute("aria-pressed"),
  className: card.getAttribute("class")
}));
return {
  leftOptions,
  rightCards,
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  selectedPendingInputOptionId: debug.state().selectedPendingInputOptionId,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert output == {
        "leftOptions": [
            {
                "id": "use-economy",
                "candidateChoice": "0",
                "pressed": "false",
                "className": "pending-input-option",
            },
            {
                "id": "use-balanced",
                "candidateChoice": "1",
                "pressed": "true",
                "className": "pending-input-option selected",
            },
        ],
        "rightCards": [
            {"index": "0", "pressed": "false", "className": "plan-card"},
            {"index": "1", "pressed": "true", "className": "plan-card selected recommended"},
        ],
        "selectedCandidateIndex": 1,
        "selectedPendingInputOptionId": "use-balanced",
        "composerValue": "use-balanced",
    }


def test_controller_renders_candidate_selection_pending_input_as_options() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "confirm_and_select"},
    candidate: {index: 1},
    data: {
      detail: {
        candidateName: "轻量应用服务器",
        candidateIndex: 1,
        summary: "开箱即用",
        totalMonthlyCost: "¥0/月",
        costItems: []
      }
    }
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {
      kind: "candidate_selection",
      prompt: "请选择购买方案",
      options: [{id: "1", label: "轻量应用服务器", summary: "开箱即用", totalMonthlyCost: "¥0/月"}]
    }
  }}}
});
const cards = all("[data-pending-input-kind]");
const options = all("[data-pending-input-option]");
const planCard = all("[data-candidate-index]")[0];
const stepText = text(all("[data-step-id]").find((step) => step.getAttribute("data-step-id") === "confirm_and_select"));
options[0].click();
const selectedPlanText = text(all("[data-candidate-index]")[0]);
return {
  cardCount: cards.length,
  optionCount: options.length,
  stepText,
  planText: selectedPlanText,
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert output["cardCount"] == 1
    assert output["optionCount"] == 1
    assert "请选择购买方案" in output["stepText"]
    assert "轻量应用服务器" in output["stepText"]
    assert "开箱即用" in output["stepText"]
    assert "¥0/月" in output["stepText"]
    assert "思考过程" in output["stepText"]
    assert output["planText"] == "已选方案 1轻量应用服务器开箱即用预估价格¥0/月"
    assert output["selectedCandidateIndex"] == 1
    assert output["composerValue"] == "选择方案1"


def test_controller_step_four_selection_ui_syncs_with_right_plan_cards() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {index: 0, name: "基础 VPC", summary: "成本最低", price: "¥0/月"},
  {index: 1, name: "VPC 含交换机", summary: "部署更完整", price: "¥0/月"}
].forEach((candidate) => applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "confirm_and_select"},
    candidate: {index: candidate.index},
    data: {
      detail: {
        candidateName: candidate.name,
        candidateIndex: candidate.index,
        summary: candidate.summary,
        totalMonthlyCost: candidate.price
      }
    }
  }}}
}));
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {
      kind: "candidate_selection",
      prompt: "请选择购买方案",
      options: [
        {id: "0", label: "基础 VPC", summary: "成本最低", totalMonthlyCost: "¥0/月"},
        {id: "1", label: "VPC 含交换机", summary: "部署更完整", totalMonthlyCost: "¥0/月"}
      ]
    }
  }}}
});
const choicesBefore = all("[data-candidate-choice]").map((choice) => ({
  index: choice.getAttribute("data-candidate-choice"),
  pressed: choice.getAttribute("aria-pressed"),
  text: text(choice)
}));
all("[data-candidate-choice]")
  .find((choice) => choice.getAttribute("data-candidate-choice") === "1")
  .click();
const choicesAfter = all("[data-candidate-choice]").map((choice) => ({
  index: choice.getAttribute("data-candidate-choice"),
  pressed: choice.getAttribute("aria-pressed"),
  className: choice.getAttribute("class")
}));
const rightCards = all("[data-candidate-index]").map((card) => ({
  index: card.getAttribute("data-candidate-index"),
  pressed: card.getAttribute("aria-pressed"),
  className: card.getAttribute("class")
}));
return {
  choicesBefore,
  choicesAfter,
  rightCards,
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert [choice["index"] for choice in output["choicesBefore"]] == ["0", "1"]
    assert "基础 VPC" in output["choicesBefore"][0]["text"]
    assert "VPC 含交换机" in output["choicesBefore"][1]["text"]
    assert output["choicesAfter"] == [
        {"index": "0", "pressed": "false", "className": "pending-input-option"},
        {"index": "1", "pressed": "true", "className": "pending-input-option selected"},
    ]
    assert output["rightCards"] == [
        {"index": "0", "pressed": "false", "className": "plan-card"},
        {"index": "1", "pressed": "true", "className": "plan-card selected recommended"},
    ]
    assert output["selectedCandidateIndex"] == 1
    assert output["composerValue"] == "选择方案1"


def test_controller_step_four_waiting_input_keeps_thinking_process_available() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
[
  {eventType: "step_started", data: {summary: "准备选择方案"}},
  {eventType: "text_delta", data: {text: "比较方案偏好"}},
  {eventType: "input_required", data: {
    kind: "candidate_selection",
    prompt: "请选择购买方案",
    options: [{id: "0", label: "基础 VPC", summary: "成本最低"}]
  }}
].forEach((event) => applyPayload({
  metadata: {iac_code: {pipeline: {
    ...event,
    status: event.eventType === "input_required" ? "input_required" : "working",
    step: {id: "confirm_and_select"}
  }}}
}));
const step = all("[data-step-id]").find((item) => item.getAttribute("data-step-id") === "confirm_and_select");
const process = all("[data-step-process]")
  .find((item) => item.getAttribute("data-step-process") === "confirm_and_select");
return {
  stepText: text(step),
  processText: text(process),
  processEvents: all("[data-step-process-event]").map((item) => item.getAttribute("data-step-process-event"))
};
"""
    )

    assert "请选择购买方案" in output["stepText"]
    assert "基础 VPC" in output["stepText"]
    assert "思考过程" in output["processText"]
    assert output["processEvents"] == ["step_started", "text_delta", "input_required"]


def test_controller_accepts_candidate_select_pending_input_alias() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "confirm_and_select"},
    candidate: {index: 1},
    data: {detail: {candidateName: "轻量应用服务器", candidateIndex: 1}}
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {
      kind: "candidate_select",
      prompt: "请选择购买方案",
      options: [{id: "1", label: "轻量应用服务器"}]
    }
  }}}
});
const cards = all("[data-pending-input-kind]");
const options = all("[data-pending-input-option]");
options[0].click();
return {
  cardCount: cards.length,
  optionCount: options.length,
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert output == {
        "cardCount": 1,
        "optionCount": 1,
        "selectedCandidateIndex": 1,
        "composerValue": "选择方案1",
    }


def test_controller_candidate_select_uses_candidate_index_when_option_id_is_not_numeric() -> None:
    output = controller_harness(
        """
controller.init();
function applyPayload(payload) {
  const next = reducers.reducePipelinePayload(debug.state(), payload);
  Object.assign(debug.state(), next);
  debug.render();
}
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "candidate_detail_shown",
    status: "working",
    step: {id: "confirm_and_select"},
    candidate: {index: 1},
    data: {
      detail: {
        candidateName: "均衡型演示方案",
        candidateIndex: 1,
        summary: "性能稳定",
        totalMonthlyCost: "¥291/月"
      }
    }
  }}}
});
applyPayload({
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {
      kind: "candidate_select",
      prompt: "请选择购买方案",
      options: [{id: "balanced-plan", label: "均衡型演示方案", candidate_index: 1}]
    }
  }}}
});
all("[data-pending-input-option]")[0].click();
const option = all("[data-pending-input-option]")[0];
const plan = all("[data-candidate-index]")[0];
return {
  optionId: option.getAttribute("data-pending-input-option"),
  candidateChoice: option.getAttribute("data-candidate-choice"),
  optionPressed: option.getAttribute("aria-pressed"),
  optionClass: option.getAttribute("class"),
  planPressed: plan.getAttribute("aria-pressed"),
  planClass: plan.getAttribute("class"),
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  selectedPendingInputOptionId: debug.state().selectedPendingInputOptionId,
  composerValue: elementById("composer-input").value
};
"""
    )

    assert output == {
        "optionId": "balanced-plan",
        "candidateChoice": "1",
        "optionPressed": "true",
        "optionClass": "pending-input-option selected",
        "planPressed": "true",
        "planClass": "plan-card selected recommended",
        "selectedCandidateIndex": 1,
        "selectedPendingInputOptionId": "balanced-plan",
        "composerValue": "选择方案1",
    }


def test_controller_candidate_choices_show_in_left_chat_and_sync_with_right_cards() -> None:
    output = controller_harness(
        """
controller.init();
debug.loadDemoCandidates();
const leftChoiceCountBefore = all("[data-pending-input-option]").length;
all("[data-pending-input-option]")[1].click();
const leftChoiceCountAfter = all("[data-pending-input-option]").length;
const planCards = all("[data-candidate-index]").map((card) => ({
  index: card.getAttribute("data-candidate-index"),
  pressed: card.getAttribute("aria-pressed")
}));
return {
  leftChoiceCountBefore,
  leftChoiceCountAfter,
  selectedCandidateIndex: debug.state().selectedCandidateIndex,
  selectedPlan: planCards.find((card) => card.index === "1"),
  prompt: reducers.promptForSelectedCandidate(debug.state())
};
"""
    )

    assert output == {
        "leftChoiceCountBefore": 2,
        "leftChoiceCountAfter": 2,
        "selectedCandidateIndex": 1,
        "selectedPlan": {
            "index": "1",
            "pressed": "true",
        },
        "prompt": "选择方案1",
    }


def test_controller_shows_normal_chat_notice_in_dialog_after_pipeline_handoff() -> None:
    output = controller_harness(
        """
controller.init();
const next = reducers.reducePipelinePayload(debug.state(), {
  metadata: {iac_code: {pipeline: {
    eventType: "pipeline_handoff_ready",
    status: "completed",
    contextId: "ctx-1",
    taskId: "task-pipeline",
    data: {action: "switch_to_normal", targetMode: "normal"}
  }}}
});
Object.assign(debug.state(), next);
debug.render();
const messages = all("[data-normal-handoff-message]").map((item) => text(item));
return {
  messages,
  composerNoticeHidden: elementById("normal-handoff-notice").hidden,
  activeTaskId: debug.state().activeTaskId,
  normalHandoffReady: debug.state().normalHandoffReady
};
"""
    )

    assert output == {
        "messages": ["部署流程已完成，已进入普通会话。可以继续追问资源、运维或变更需求。"],
        "composerNoticeHidden": True,
        "activeTaskId": "",
        "normalHandoffReady": True,
    }


def test_controller_renders_debug_session_info_for_issue_reports() -> None:
    output = controller_harness(
        """
controller.init();
Object.assign(debug.state(), {
  contextId: "ctx-1",
  pipelineTaskId: "task-pipeline",
  activeTaskId: "task-active",
  lastSequence: 42,
  status: "working",
  normalHandoffReady: false
});
debug.render();
const fields = all("[data-debug-session-field]").map((field) => ({
  key: field.getAttribute("data-debug-session-field"),
  text: text(field)
}));
return {fields};
"""
    )

    assert output["fields"] == [
        {"key": "serverUrl", "text": "Server URLhttp://127.0.0.1:41299"},
        {"key": "cwd", "text": "CWD/workspace"},
        {"key": "iacCodeModel", "text": "Model"},
        {"key": "contextId", "text": "Context IDctx-1"},
        {"key": "pipelineTaskId", "text": "Pipeline Tasktask-pipeline"},
        {"key": "activeTaskId", "text": "Active Tasktask-active"},
        {"key": "lastSequence", "text": "Last Sequence42"},
        {"key": "status", "text": "Statusworking"},
        {"key": "handoff", "text": "Normal Handoff否"},
        {"key": "logs", "text": "Logs默认 ~/.iac-code/logs，或 IAC_CODE_CONFIG_DIR/logs"},
    ]


def test_controller_plan_card_selection_updates_left_candidate_choice() -> None:
    output = controller_harness(
        """
controller.init();
debug.loadDemoCandidates();
all("[data-candidate-index]")[1].click();
return {
  leftChoices: all("[data-candidate-choice]").map((choice) => ({
    index: choice.getAttribute("data-candidate-choice"),
    pressed: choice.getAttribute("aria-pressed")
  })),
  rightCards: all("[data-candidate-index]").map((card) => ({
    index: card.getAttribute("data-candidate-index"),
    pressed: card.getAttribute("aria-pressed")
  })),
  prompt: reducers.promptForSelectedCandidate(debug.state())
};
"""
    )

    assert output == {
        "leftChoices": [
            {"index": "0", "pressed": "false"},
            {"index": "1", "pressed": "true"},
        ],
        "rightCards": [
            {"index": "0", "pressed": "false"},
            {"index": "1", "pressed": "true"},
        ],
        "prompt": "选择方案1",
    }


def test_controller_reports_sse_error_event_as_failed_send() -> None:
    output = controller_harness(
        """
controller.init();
elementById("composer-input").value = "继续部署";
global.fetch = async () => ({
  ok: true,
  status: 200,
  body: null,
  text: async () => 'data: {"ok": false, "error": "upstream timed out"}\\n\\n'
});
await controller.sendComposerMessage();
return {
  alertText: elementById("status-alert").textContent,
  alertKind: elementById("status-alert").getAttribute("data-kind"),
  debug: debugText()
};
"""
    )

    assert output["alertText"] == "消息发送失败：upstream timed out"
    assert output["alertKind"] == "error"
    assert "upstream timed out" in output["debug"]


def test_controller_yields_between_sse_blocks_so_streaming_can_paint_incrementally() -> None:
    output = controller_harness(
        """
controller.init();
debug.state().progressUi.variant = "a";
elementById("composer-input").value = "创建 VPC";
let paintCount = 0;
global.requestAnimationFrame = (callback) => {
  paintCount += 1;
  return setTimeout(() => callback(Date.now()), 0);
};
global.cancelAnimationFrame = (id) => clearTimeout(id);
const encoder = new TextEncoder();
let readCount = 0;
global.fetch = async () => ({
  ok: true,
  status: 200,
  body: {
    getReader() {
      return {
        async read() {
          readCount += 1;
          if (readCount === 1) {
            return {
              done: false,
              value: encoder.encode([
                'data: {"metadata":{"iac_code":{"pipeline":' +
                  '{"eventType":"step_started","status":"working","step":{"id":"intent_parsing"},' +
                  '"data":{"summary":"开始理解需求"}}}}}',
                'data: {"metadata":{"iac_code":{"pipeline":' +
                  '{"eventType":"text_delta","status":"working","step":{"id":"intent_parsing"},' +
                  '"data":{"text":"正在分析预算"}}}}}'
              ].join("\\n\\n") + "\\n\\n")
            };
          }
          return {done: true};
        },
        async cancel() {}
      };
    }
  }
});
await controller.sendComposerMessage();
return {
  paintCount,
  cardKinds: all("[data-step-event-kind]").map((item) => item.getAttribute("data-step-event-kind"))
};
"""
    )

    assert output["paintCount"] >= 2
    assert output["cardKinds"] == ["step_started", "text_delta"]


def test_reducer_deep_clones_existing_permission_and_diagnostics() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.permission = {decision: {allowed: true}};
state.diagnostics = {
  requests: [{meta: {id: "req-1"}}],
  sse: [{meta: {id: "sse-1"}}],
  snapshots: [{meta: {id: "snap-1"}}]
};
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "step_completed",
    step: {id: "architecture_planning", status: "completed"}
  }}}
});
state.permission.decision.allowed = false;
state.diagnostics.requests[0].meta.id = "mutated";
state.diagnostics.sse[0].meta.id = "mutated";
state.diagnostics.snapshots[0].meta.id = "mutated";
return {
  permissionAllowed: next.permission.decision.allowed,
  requestId: next.diagnostics.requests[0].meta.id,
  sseId: next.diagnostics.sse[0].meta.id,
  snapshotId: next.diagnostics.snapshots[0].meta.id
};
"""
    )

    assert output == {
        "permissionAllowed": True,
        "requestId": "req-1",
        "sseId": "sse-1",
        "snapshotId": "snap-1",
    }


def test_candidate_from_display_item_deep_clones_cost_item_nested_fields() -> None:
    output = reducer_harness(
        """
const source = {
  candidateName: "方案",
  candidateIndex: 0,
  costItems: [{name: "ecs", detail: {region: "cn-hangzhou"}}]
};
const candidate = reducers.candidateFromDisplayItem(source);
source.costItems[0].detail.region = "mutated";
return {
  name: candidate.name,
  region: candidate.costItems[0].detail.region
};
"""
    )

    assert output == {
        "name": "方案",
        "region": "cn-hangzhou",
    }


def test_reducer_sets_and_clears_realtime_pending_input() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
const waiting = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    taskId: "task-1",
    contextId: "ctx-1",
    input: {
      inputId: "ask-1",
      kind: "ask_user_question",
      question: "请选择部署目标",
      options: [{id: "nginx", label: "Nginx 网站"}]
    }
  }}}
});
const received = reducers.reducePipelinePayload(waiting, {
  metadata: {iac_code: {pipeline: {
    eventType: "input_received",
    status: "working",
    taskId: "task-1",
    contextId: "ctx-1"
  }}}
});
return {
  prompt: waiting.pendingInput.prompt,
  optionLabel: waiting.pendingInput.options[0].label,
  candidateCount: waiting.candidates.length,
  originalPending: state.pendingInput,
  waitingStatus: waiting.status,
  cleared: received.pendingInput === null
};
"""
    )

    assert output == {
        "prompt": "请选择部署目标",
        "optionLabel": "Nginx 网站",
        "candidateCount": 0,
        "originalPending": None,
        "waitingStatus": "waiting_input",
        "cleared": True,
    }


def test_reducer_does_not_turn_pending_input_data_options_into_candidates() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    data: {
      question: "请选择部署目标",
      options: [{id: "nginx", label: "Nginx 网站"}]
    }
  }}}
});
return {
  candidateCount: next.candidates.length,
  prompt: next.pendingInput.prompt,
  optionLabel: next.pendingInput.options[0].label
};
"""
    )

    assert output == {
        "candidateCount": 0,
        "prompt": "请选择部署目标",
        "optionLabel": "Nginx 网站",
    }


def test_reducer_collects_candidate_selection_options_from_pending_input() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    step: {id: "confirm_and_select"},
    data: {
      kind: "candidate_selection",
      prompt: "请选择购买方案",
      options: [{
        id: "1",
        label: "标准 VPC",
        description: "在 cn-hangzhou 创建一个标准 VPC，使用默认网段 172.16.0.0/12。",
        price: "¥0/月"
      }]
    }
  }}}
});
return {
  candidateCount: next.candidates.length,
  name: next.candidates[0] && next.candidates[0].name,
  index: next.candidates[0] && next.candidates[0].candidateIndex,
  summary: next.candidates[0] && next.candidates[0].summary,
  cost: next.candidates[0] && next.candidates[0].totalMonthlyCost,
  pendingPrompt: next.pendingInput.prompt
};
"""
    )

    assert output == {
        "candidateCount": 1,
        "name": "标准 VPC",
        "index": 1,
        "summary": "在 cn-hangzhou 创建一个标准 VPC，使用默认网段 172.16.0.0/12。",
        "cost": "¥0/月",
        "pendingPrompt": "请选择购买方案",
    }


def test_reducer_deep_clones_realtime_pending_input_payload() -> None:
    output = reducer_harness(
        """
const input = {
  question: "请选择部署目标",
  extra: {source: "planner"},
  options: [{id: "nginx", label: "Nginx 网站", meta: {score: 1}}]
};
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  metadata: {iac_code: {pipeline: {
    eventType: "input_required",
    status: "input_required",
    taskId: "task-1",
    contextId: "ctx-1",
    input
  }}}
});
input.extra.source = "mutated";
input.options[0].meta.score = 99;
return {
  prompt: next.pendingInput.prompt,
  source: next.pendingInput.extra.source,
  score: next.pendingInput.options[0].meta.score
};
"""
    )

    assert output == {
        "prompt": "请选择部署目标",
        "source": "planner",
        "score": 1,
    }


def test_reducer_handles_snake_case_input_required_envelope() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  metadata: {iac_code: {pipeline: {
    event_type: "input_required",
    status: "input_required",
    task_id: "task-1",
    context_id: "ctx-1",
    pending_input: {
      question: "请选择部署目标",
      options: [{id: "nginx", label: "Nginx 网站"}]
    }
  }}}
});
return {
  taskId: next.pipelineTaskId,
  contextId: next.contextId,
  status: next.status,
  prompt: next.pendingInput && next.pendingInput.prompt
};
"""
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "waiting_input",
        "prompt": "请选择部署目标",
    }


def test_reducer_extracts_realtime_envelope_from_a2a_status_update_wrapper() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  result: {
    statusUpdate: {
      metadata: {iac_code: {pipeline: {
        eventType: "input_required",
        status: "input_required",
        taskId: "task-1",
        contextId: "ctx-1",
        input: {
          question: "请选择部署目标",
          options: [{id: "nginx", label: "Nginx 网站"}]
        }
      }}}
    }
  }
});
return {
  taskId: next.pipelineTaskId,
  contextId: next.contextId,
  status: next.status,
  prompt: next.pendingInput && next.pendingInput.prompt
};
"""
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "waiting_input",
        "prompt": "请选择部署目标",
    }


def test_reducer_restores_pipeline_state_snapshot_and_applies_events() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  snapshot: {
    taskId: "task-1",
    contextId: "ctx-1",
    lastSequence: 7,
    status: "working",
    steps: [{id: "architecture_planning", status: "completed"}]
  },
  events: [{
    eventType: "step_completed",
    status: "working",
    taskId: "task-1",
    contextId: "ctx-1",
    sequence: 8,
    step: {id: "evaluate_candidates", status: "completed"}
  }]
});
return {
  taskId: next.pipelineTaskId,
  contextId: next.contextId,
  lastSequence: next.lastSequence,
  architectureStatus: next.steps.architecture_planning.status,
  evaluateStatus: next.steps.evaluate_candidates.status,
  evaluateEvents: next.steps.evaluate_candidates.events.length
};
"""
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "lastSequence": 8,
        "architectureStatus": "completed",
        "evaluateStatus": "completed",
        "evaluateEvents": 1,
    }


def test_reducer_does_not_roll_back_last_sequence_from_replay_event() -> None:
    output = reducer_harness(
        """
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), {
  snapshot: {
    task_id: "task-1",
    context_id: "ctx-1",
    last_sequence: "10",
    status: "working"
  },
  events: [{
    event_type: "step_completed",
    sequence: 8,
    step: {id: "evaluate_candidates", status: "completed"}
  }]
});
return {
  lastSequence: next.lastSequence,
  evaluateEvents: next.steps.evaluate_candidates.events.length
};
"""
    )

    assert output == {
        "lastSequence": 10,
        "evaluateEvents": 1,
    }


def test_reducer_snapshot_pending_input_null_clears_stale_pending_input() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.pendingInput = {prompt: "旧问题", options: [{id: "old", label: "旧选项"}]};
const next = reducers.reducePipelinePayload(state, {
  snapshot: {status: "working", pendingInput: null}
});
return {
  originalPrompt: state.pendingInput.prompt,
  nextPending: next.pendingInput
};
"""
    )

    assert output == {
        "originalPrompt": "旧问题",
        "nextPending": None,
    }


def test_reducer_snapshot_normal_handoff_switches_to_normal_mode() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.activeTaskId = "pipeline-task";
const next = reducers.reducePipelinePayload(state, {
  snapshot: {
    status: "completed",
    normalHandoff: {action: "switch_to_normal", targetMode: "normal"}
  }
});
return {
  normalHandoffReady: next.normalHandoffReady,
  activeTaskId: next.activeTaskId,
  status: next.status,
  originalActiveTaskId: state.activeTaskId
};
"""
    )

    assert output == {
        "normalHandoffReady": True,
        "activeTaskId": "",
        "status": "completed",
        "originalActiveTaskId": "pipeline-task",
    }


def test_reducer_snapshot_snake_case_normal_handoff_switches_to_normal_mode() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.activeTaskId = "pipeline-task";
const next = reducers.reducePipelinePayload(state, {
  snapshot: {
    status: "completed",
    normal_handoff: {action: "switch_to_normal", target_mode: "normal"}
  }
});
return {
  normalHandoffReady: next.normalHandoffReady,
  activeTaskId: next.activeTaskId,
  status: next.status,
  originalActiveTaskId: state.activeTaskId
};
"""
    )

    assert output == {
        "normalHandoffReady": True,
        "activeTaskId": "",
        "status": "completed",
        "originalActiveTaskId": "pipeline-task",
    }


def test_reducer_attaches_pipeline_scoped_events_to_current_step() -> None:
    output = reducer_harness(
        """
let state = reducers.createInitialState({});
[
  {
    metadata: {iac_code: {pipeline: {
      eventType: "step_started",
      status: "working",
      step: {id: "deploying"},
      data: {summary: "开始部署"}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "text_delta",
      status: "working",
      scope: "pipeline",
      data: {text: "开始部署流程"}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "permission_requested",
      status: "working",
      scope: "pipeline",
      data: {toolName: "ros_stack", reason: "创建资源栈"}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "tool_result",
      status: "working",
      scope: "pipeline",
      data: {toolName: "ros_stack", result: {stackId: "stack-1", stackStatus: "CREATE_COMPLETE"}}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "step_completed",
      status: "completed",
      step: {id: "deploying"},
      data: {conclusion: {summary: "部署完成"}}
    }}}
  }
].forEach((payload) => {
  state = reducers.reducePipelinePayload(state, payload);
});
return {
  currentStepId: state.currentStepId,
  deployingEvents: state.steps.deploying.events.map((event) => event.eventType)
};
"""
    )

    assert output == {
        "currentStepId": "deploying",
        "deployingEvents": ["step_started", "text_delta", "permission_requested", "tool_result", "step_completed"],
    }


def test_reducer_does_not_attach_pipeline_scoped_events_to_completed_step() -> None:
    output = reducer_harness(
        """
let state = reducers.createInitialState({});
[
  {
    metadata: {iac_code: {pipeline: {
      eventType: "step_started",
      status: "working",
      step: {id: "deploying"},
      data: {summary: "开始部署"}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "step_completed",
      status: "completed",
      step: {id: "deploying"},
      data: {conclusion: {summary: "部署完成"}}
    }}}
  },
  {
    metadata: {iac_code: {pipeline: {
      eventType: "text_delta",
      status: "completed",
      scope: "pipeline",
      data: {text: "流程结束后的普通消息"}
    }}}
  }
].forEach((payload) => {
  state = reducers.reducePipelinePayload(state, payload);
});
return {
  currentStepId: state.currentStepId,
  deployingEvents: state.steps.deploying.events.map((event) => event.eventType || event.event_type)
};
"""
    )

    assert output == {
        "currentStepId": "deploying",
        "deployingEvents": ["step_started", "step_completed"],
    }


def test_reducer_applies_raw_snapshot_like_payload_with_snake_case_aliases() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.activeTaskId = "pipeline-task";
state.pendingInput = {prompt: "旧问题", options: [{id: "old", label: "旧选项"}]};
const next = reducers.reducePipelinePayload(state, {
  status: "input_required",
  task_id: "task-1",
  context_id: "ctx-1",
  last_sequence: "12",
  pending_input: {
    question: "请选择部署目标",
    options: [{id: "nginx", label: "Nginx 网站"}]
  },
  normal_handoff: {action: "switch_to_normal", target_mode: "normal"}
});
return {
  status: next.status,
  taskId: next.pipelineTaskId,
  contextId: next.contextId,
  lastSequence: next.lastSequence,
  prompt: next.pendingInput && next.pendingInput.prompt,
  activeTaskId: next.activeTaskId,
  normalHandoffReady: next.normalHandoffReady,
  originalPrompt: state.pendingInput.prompt
};
"""
    )

    assert output == {
        "status": "waiting_input",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "lastSequence": 12,
        "prompt": "请选择部署目标",
        "activeTaskId": "",
        "normalHandoffReady": True,
        "originalPrompt": "旧问题",
    }


def test_reducer_does_not_retain_mutable_candidate_payload_references() -> None:
    output = reducer_harness(
        """
const costItems = [{name: "ecs"}];
const payload = {snapshot: {display: {candidateDetails: [{
  candidateName: "方案",
  candidateIndex: 0,
  costItems
}]}}};
const next = reducers.reducePipelinePayload(reducers.createInitialState({}), payload);
payload.snapshot.display.candidateDetails[0].candidateName = "被污染";
costItems[0].name = "mutated";
return {
  name: next.candidates[0].name,
  costItemName: next.candidates[0].costItems[0].name
};
"""
    )

    assert output == {
        "name": "方案",
        "costItemName": "ecs",
    }


def test_upsert_candidate_does_not_mutate_original_state() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{name: "旧方案", candidateIndex: 0, costItems: []}];
const next = reducers.upsertCandidate(state, {
  name: "新方案",
  candidateIndex: 0,
  totalMonthlyCost: "CNY 80",
  costItems: [{name: "ecs"}]
});
return {
  sameState: next === state,
  sameCandidates: next.candidates === state.candidates,
  originalName: state.candidates[0].name,
  originalCost: state.candidates[0].totalMonthlyCost || "",
  nextName: next.candidates[0].name,
  nextCost: next.candidates[0].totalMonthlyCost,
  nextCostItem: next.candidates[0].costItems[0].name
};
"""
    )

    assert output == {
        "sameState": False,
        "sameCandidates": False,
        "originalName": "旧方案",
        "originalCost": "",
        "nextName": "新方案",
        "nextCost": "CNY 80",
        "nextCostItem": "ecs",
    }


def test_upsert_candidate_deduplicates_numeric_string_candidate_index() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{name: "旧方案", candidateIndex: 1, costItems: []}];
const next = reducers.upsertCandidate(state, {
  name: "新方案",
  candidateIndex: "1",
  totalMonthlyCost: "¥0/月"
});
return {
  count: next.candidates.length,
  index: next.candidates[0].candidateIndex,
  name: next.candidates[0].name,
  originalName: state.candidates[0].name
};
"""
    )

    assert output == {
        "count": 1,
        "index": 1,
        "name": "新方案",
        "originalName": "旧方案",
    }


def test_upsert_candidate_merges_indexed_detail_into_same_name_placeholder() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{
  name: "标准 VPC 网络",
  candidateIndex: null,
  summary: "",
  totalMonthlyCost: "",
  costItems: []
}];
const next = reducers.upsertCandidate(state, {
  name: "标准 VPC 网络",
  candidateIndex: 0,
  summary: "仅创建 VPC，作为后续子网和云资源的网络容器。",
  totalMonthlyCost: "¥33.89/月",
  costItems: [{name: "VPC", monthly_cost: "免费"}]
});
const candidate = next.candidates.find((item) => item.candidateIndex === 0) || next.candidates[0];
return {
  count: next.candidates.length,
  index: candidate.candidateIndex,
  name: candidate.name,
  summary: candidate.summary,
  cost: candidate.totalMonthlyCost,
  costItem: candidate.costItems[0] && candidate.costItems[0].name
};
"""
    )

    assert output == {
        "count": 1,
        "index": 0,
        "name": "标准 VPC 网络",
        "summary": "仅创建 VPC，作为后续子网和云资源的网络容器。",
        "cost": "¥33.89/月",
        "costItem": "VPC",
    }


def test_upsert_candidate_does_not_overwrite_existing_detail_with_empty_fields() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState({});
state.candidates = [{
  name: "VPC 含可用区交换机",
  candidateIndex: 1,
  summary: "创建 VPC 及一个可用区交换机，开箱即用。",
  totalMonthlyCost: "¥0/月",
  costItems: [{name: "VPC", monthly_cost: "免费"}]
}];
const next = reducers.upsertCandidate(state, {
  name: "VPC 含可用区交换机",
  candidateIndex: 1,
  summary: "",
  totalMonthlyCost: "",
  costItems: []
});
return {
  count: next.candidates.length,
  summary: next.candidates[0].summary,
  cost: next.candidates[0].totalMonthlyCost,
  costItemCount: next.candidates[0].costItems.length
};
"""
    )

    assert output == {
        "count": 1,
        "summary": "创建 VPC 及一个可用区交换机，开箱即用。",
        "cost": "¥0/月",
        "costItemCount": 1,
    }


def test_extract_pipeline_envelope_handles_snapshot_metadata_wrapper() -> None:
    output = reducer_harness(
        """
const envelope = reducers.extractPipelineEnvelope({
  snapshot: {
    metadata: {iac_code: {pipeline: {
      eventType: "step_completed",
      taskId: "task-1",
      contextId: "ctx-1",
      step: {id: "architecture_planning"}
    }}}
  }
});
return {
  taskId: envelope.taskId,
  contextId: envelope.contextId,
  stepId: envelope.step.id
};
"""
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "stepId": "architecture_planning",
    }


def test_reducer_clears_active_task_on_normal_handoff() -> None:
    output = reducer_harness(
        """
const state = reducers.createInitialState();
state.pipelineTaskId = "pipeline-task";
state.activeTaskId = "pipeline-task";
const next = reducers.reducePipelinePayload(state, {
  metadata: {iac_code: {pipeline: {
    eventType: "pipeline_handoff_ready",
    taskId: "pipeline-task",
    contextId: "ctx-1",
    status: "completed",
    data: {action: "switch_to_normal", targetMode: "normal"}
  }}}
});
return {
  normalHandoffReady: next.normalHandoffReady,
  activeTaskId: next.activeTaskId,
  contextId: next.contextId
};
"""
    )

    assert output == {
        "normalHandoffReady": True,
        "activeTaskId": "",
        "contextId": "ctx-1",
    }
