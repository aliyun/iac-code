"""Hidden prompt snapshot export command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, cast

from iac_code.agent.message import RECALLED_MEMORY_MARKER
from iac_code.agent.system_prompt import DYNAMIC_BOUNDARY
from iac_code.i18n import _
from iac_code.utils.file_security import ensure_private_file


async def prompt_command(context=None, **kwargs) -> str:
    repl = getattr(context, "repl", None) if context is not None else None
    if repl is None:
        return _("Prompt command requires a REPL context.")

    ensure_pipeline = getattr(repl, "ensure_pipeline_restored_for_prompt", None)
    if callable(ensure_pipeline):
        await ensure_pipeline()

    try:
        snapshot = _pipeline_prompt_snapshot(repl) or build_prompt_snapshot(repl)
        path = export_prompt_html(
            repl,
            output_dir=kwargs.get("output_dir"),
            snapshot=snapshot,
            prefer_session_path=callable(kwargs.get("browser_opener")),
        )
    except Exception as exc:
        return _("Failed to export prompt: {error}").format(error=exc)

    try:
        _open_prompt_export(path, kwargs.get("browser_opener"))
    except Exception as exc:
        return _("Prompt exported: {path}\nFailed to open it automatically: {error}").format(path=path, error=exc)

    return _("Prompt exported and opened: {path}").format(path=path)


def export_prompt_html(
    repl: object,
    *,
    output_dir: Path | str | None = None,
    snapshot: dict[str, Any] | None = None,
    prefer_session_path: bool = False,
) -> Path:
    snapshot = snapshot or build_prompt_snapshot(repl)
    html = render_prompt_html(snapshot)
    if output_dir is None and prefer_session_path:
        path = _prompt_html_path(repl)
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        directory = Path(output_dir) if output_dir is not None else Path(tempfile.mkdtemp(prefix="iac-code-prompt-"))
        directory.mkdir(parents=True, exist_ok=True)
        session_id = _safe_filename(str(snapshot["metadata"].get("session_id") or "session"))
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"iac-code-prompt-{session_id}-{timestamp}.html"
    path.write_text(html, encoding="utf-8", newline="\n")
    ensure_private_file(path)
    return path


def build_prompt_snapshot(repl: object) -> dict[str, Any]:
    agent_loop = getattr(repl, "_agent_loop", None)
    if agent_loop is None:
        raise RuntimeError(_("Prompt export is only available in interactive mode."))

    last_request = _last_provider_request(agent_loop)
    source = _("Last main-model request") if last_request else _("Current runtime state")
    system_prompt = str(last_request.get("system_prompt") or _current_system_prompt(repl, agent_loop))
    provider_messages = (
        list(last_request.get("provider_messages") or [])
        if "provider_messages" in last_request
        else _provider_messages(agent_loop)
    )
    tools = list(last_request.get("tools") or []) if "tools" in last_request else _tool_definitions(agent_loop)
    status = _status_snapshot(repl)
    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": status.get("session_id") or getattr(agent_loop, "session_id", ""),
        "provider": status.get("provider", ""),
        "model": status.get("model", ""),
        "cwd": status.get("cwd", ""),
        "source": source,
    }
    return {
        "metadata": metadata,
        "system_prompt": system_prompt,
        "system_sections": _split_system_prompt(system_prompt),
        "provider_messages": provider_messages,
        "tools": tools,
        "memory_sections": _memory_sections(repl),
    }


def _pipeline_prompt_snapshot(repl: object) -> dict[str, Any] | None:
    pipeline = getattr(repl, "_pipeline", None)
    get_prompt_contexts = getattr(pipeline, "get_prompt_contexts", None)
    if not callable(get_prompt_contexts):
        return None
    contexts = list(get_prompt_contexts() or [])
    if not contexts:
        return None

    status = _status_snapshot(repl)
    sections = _pipeline_sections(contexts)
    system_prompt = "\n\n".join(section["content"] for section in sections)
    metadata = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": status.get("session_id") or getattr(repl, "_session_id", ""),
        "provider": status.get("provider", ""),
        "model": status.get("model", ""),
        "cwd": status.get("cwd") or getattr(repl, "_original_cwd", ""),
        "source": _("Pipeline prompt contexts"),
    }
    return {
        "metadata": metadata,
        "system_prompt": system_prompt,
        "system_sections": sections,
        "provider_messages": [],
        "tools": [],
        "memory_sections": [],
    }


def _pipeline_sections(contexts: list[object]) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for item in contexts:
        title = _pipeline_context_title(item)
        sections.append(
            {
                "title": title,
                "content": _format_pipeline_context(item, title),
                "zone": _("pipeline"),
            }
        )
    return sections


def _pipeline_context_title(item: object) -> str:
    scope = str(getattr(item, "scope", "") or "")
    step_id = str(getattr(item, "step_id", "") or "")
    if scope == "candidate":
        candidate_index = getattr(item, "candidate_index", None)
        if candidate_index is None:
            number = 0
        else:
            try:
                number = int(candidate_index) + 1
            except (TypeError, ValueError):
                number = 0
        title = _("Candidate #{index}").format(index=number)
        candidate_name = str(getattr(item, "candidate_name", "") or "")
        if candidate_name:
            title = _("{title} - {name}").format(title=title, name=candidate_name)
        if step_id:
            title = _("{title} / {step}").format(title=title, step=step_id)
        return title
    return _("Step {step}").format(step=step_id)


def _format_pipeline_context(item: object, title: str) -> str:
    lines = [title]
    session_id = str(getattr(item, "agent_loop_session_id", "") or "")
    if session_id:
        lines.append(_("AgentLoop session: {session_id}").format(session_id=session_id))
    system_prompt = str(getattr(item, "system_prompt", "") or "")
    lines.extend(["", _("System Prompt:"), system_prompt])
    initial_prompt = str(getattr(item, "initial_prompt", "") or "")
    messages = list(getattr(item, "messages", []) or [])
    if initial_prompt and not messages:
        lines.extend(["", _("Initial User Prompt:"), initial_prompt])
    lines.append("")
    lines.append(_("Messages:"))
    if not messages:
        lines.append(_("(none)"))
    for message in messages:
        role = str(getattr(message, "role", "") or "message")
        lines.append("[{role}]".format(role=role))
        lines.append(_message_text(message))
    return "\n".join(lines)


def _message_text(message: object) -> str:
    get_text = getattr(message, "get_text", None)
    if callable(get_text):
        text = get_text()
        if text:
            return str(text)
    return str(getattr(message, "content", "") or "")


def render_prompt_html(snapshot: dict[str, Any]) -> str:
    metadata = snapshot.get("metadata") or {}
    metadata_rows = "\n".join(
        _metadata_item(label, str(metadata.get(key, "") or ""))
        for label, key in [
            (_("Generated"), "generated_at"),
            (_("Session"), "session_id"),
            (_("Provider"), "provider"),
            (_("Model"), "model"),
            (_("CWD"), "cwd"),
            (_("Source"), "source"),
        ]
    )
    system_sections = "\n".join(
        _content_card(section["title"], section["content"], badge=section.get("zone", ""))
        for section in snapshot.get("system_sections", [])
    )
    provider_messages = "\n".join(
        _message_card(index, message) for index, message in enumerate(snapshot.get("provider_messages", []), start=1)
    )
    tools = "\n".join(_tool_card(tool) for tool in snapshot.get("tools", []))
    raw_system_prompt = _content_card(
        _("Raw Full System Prompt"),
        str(snapshot.get("system_prompt", "")),
        collapsed=True,
    )
    all_tab = _render_all_tab(snapshot)
    system_tab = "{system_sections}{raw_system_prompt}".format(
        system_sections=system_sections or '<p class="empty">{}</p>'.format(escape(_("System prompt is empty."))),
        raw_system_prompt=raw_system_prompt,
    )
    messages_tab = provider_messages or '<p class="empty">{}</p>'.format(escape(_("No provider messages yet.")))
    tools_tab = tools or '<p class="empty">{}</p>'.format(escape(_("No tools are currently registered.")))
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_title}</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #18202b;
  --muted: #657184;
  --line: #d9dee7;
  --accent: #176b87;
  --code: #f0f3f7;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #101418;
    --panel: #171d24;
    --text: #e7edf4;
    --muted: #9aa8b8;
    --line: #2a3440;
    --accent: #6cc5d9;
    --code: #0d1117;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
main {{
  width: min(1180px, calc(100vw - 40px));
  margin: 28px auto 48px;
}}
h1 {{
  margin: 0 0 4px;
  font-size: 28px;
  letter-spacing: 0;
}}
h2 {{
  margin: 28px 0 12px;
  font-size: 18px;
  letter-spacing: 0;
}}
.subtitle {{
  margin: 0 0 18px;
  color: var(--muted);
}}
.metadata {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin: 18px 0 26px;
}}
.tabs {{
  display: flex;
  gap: 6px;
  border-bottom: 1px solid var(--line);
  margin: 8px 0 16px;
  overflow-x: auto;
}}
.tab-button {{
  appearance: none;
  border: 0;
  border-bottom: 3px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  padding: 11px 14px 10px;
  font: inherit;
  font-weight: 650;
  white-space: nowrap;
}}
.tab-button[aria-selected="true"] {{
  color: var(--accent);
  border-bottom-color: var(--accent);
}}
.tab-panel {{
  display: none;
}}
.tab-panel.active {{
  display: block;
}}
.assembly-list {{
  display: grid;
  gap: 10px;
  margin: 10px 0;
}}
.assembly-step {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 14px;
}}
.assembly-step-title {{
  color: var(--accent);
  font-weight: 700;
  margin-bottom: 4px;
}}
.assembly-step-body {{
  color: var(--text);
}}
.assembly-step-meta {{
  margin-top: 7px;
  color: var(--muted);
  font-size: 12px;
}}
.inline-tab-link {{
  appearance: none;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--code);
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 3px 8px;
}}
.meta-item, .card {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}}
.meta-item {{
  padding: 10px 12px;
}}
.meta-label {{
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}}
.meta-value {{
  margin-top: 3px;
  word-break: break-word;
}}
.card {{
  margin: 10px 0;
  overflow: hidden;
}}
.card-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  color: var(--accent);
  font-weight: 650;
}}
.badge {{
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}}
details.card > summary {{
  cursor: pointer;
  list-style: none;
}}
details.card > summary::-webkit-details-marker {{
  display: none;
}}
pre {{
  margin: 0;
  padding: 14px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--code);
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
.empty {{
  margin: 0;
  padding: 14px;
  color: var(--muted);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}}
</style>
</head>
<body>
<main>
  <h1>{page_title}</h1>
  <p class="subtitle">
    {subtitle}
  </p>
  <section class="metadata">
    {metadata_rows}
  </section>
  <nav class="tabs" role="tablist" aria-label="{tab_aria_label}">
    {all_tab_button}
    {system_tab_button}
    {messages_tab_button}
    {tools_tab_button}
  </nav>
  {all_panel}
  {system_panel}
  {messages_panel}
  {tools_panel}
</main>
<script>
const buttons = document.querySelectorAll("[data-tab-target]");
const panels = document.querySelectorAll("[data-tab-panel]");
function openTab(target) {{
    for (const item of buttons) {{
    item.setAttribute("aria-selected", String(item.dataset.tabTarget === target));
    }}
    for (const panel of panels) {{
      panel.classList.toggle("active", panel.dataset.tabPanel === target);
    }}
}}
for (const button of buttons) {{
  button.addEventListener("click", () => {{
    openTab(button.dataset.tabTarget);
  }});
}}
for (const link of document.querySelectorAll("[data-open-tab]")) {{
  link.addEventListener("click", () => openTab(link.dataset.openTab));
}}
</script>
</body>
</html>
""".format(
        html_title=escape(_("IAC-CODE Prompt Snapshot")),
        page_title=escape(_("Prompt Snapshot")),
        subtitle=escape(
            _(
                "A local diagnostic view of the current main-model prompt state. "
                "This export does not trigger memory recall."
            )
        ),
        tab_aria_label=escape(_("Prompt snapshot sections")),
        metadata_rows=metadata_rows,
        all_tab_button=_tab_button("all", _("ALL"), selected=True),
        system_tab_button=_tab_button("system", _("System Prompt")),
        messages_tab_button=_tab_button("messages", _("Provider Messages")),
        tools_tab_button=_tab_button("tools", _("Tools")),
        all_panel=_tab_panel("all", all_tab, active=True),
        system_panel=_tab_panel("system", system_tab),
        messages_panel=_tab_panel("messages", messages_tab),
        tools_panel=_tab_panel("tools", tools_tab),
    )


def _current_system_prompt(repl: object, agent_loop: object) -> str:
    builder = getattr(repl, "_build_current_system_prompt", None)
    if callable(builder):
        prompt = builder()
        if isinstance(prompt, str):
            return prompt
    return str(getattr(agent_loop, "system_prompt", "") or "")


def _status_snapshot(repl: object) -> dict[str, Any]:
    get_status = getattr(repl, "get_status_snapshot", None)
    if not callable(get_status):
        return {}
    try:
        snapshot = get_status()
    except Exception:
        return {}
    return snapshot if isinstance(snapshot, dict) else {}


def _provider_messages(agent_loop: object) -> list[dict[str, Any]]:
    getter = getattr(agent_loop, "_get_provider_messages", None)
    if not callable(getter):
        return []
    try:
        messages = getter()
    except Exception:
        return []
    return [_message_snapshot(message) for message in messages]


def _last_provider_request(agent_loop: object) -> dict[str, Any]:
    getter = getattr(agent_loop, "get_last_provider_request_snapshot", None)
    if not callable(getter):
        return {}
    try:
        snapshot = getter()
    except Exception:
        return {}
    if not isinstance(snapshot, dict) or not snapshot:
        return {}
    return {
        "system_prompt": str(snapshot.get("system_prompt") or ""),
        "provider_messages": [_message_snapshot(message) for message in snapshot.get("provider_messages") or []],
        "tools": [_tool_snapshot(tool) for tool in snapshot.get("tools") or []],
    }


def _tool_definitions(agent_loop: object) -> list[dict[str, Any]]:
    getter = getattr(agent_loop, "_get_tool_definitions", None)
    if not callable(getter):
        return []
    try:
        tools = getter()
    except Exception:
        return []
    return [_tool_snapshot(tool) for tool in tools]


def _tool_snapshot(tool: object) -> dict[str, Any]:
    if isinstance(tool, Mapping):
        tool_map = cast(Mapping[str, Any], tool)
        return {
            "name": str(tool_map.get("name") or ""),
            "description": str(tool_map.get("description") or ""),
            "input_schema": tool_map.get("input_schema") or {},
        }
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "input_schema": getattr(tool, "input_schema", {}) or {},
    }


def _memory_sections(repl: object) -> list[dict[str, str]]:
    memory_context = getattr(repl, "_memory_context", None)
    sections: list[dict[str, str]] = []
    for title, attr in [
        (_("Instruction Memory"), "instruction_memory_content"),
        (_("Memory Mechanics"), "memory_mechanics_content"),
    ]:
        content = str(getattr(memory_context, attr, "") or "").strip()
        if content:
            sections.append({"title": title, "content": content})
    return sections


def _message_snapshot(message: object) -> dict[str, Any]:
    return {
        "role": str(getattr(message, "role", "") or ""),
        "content": _content_snapshot(getattr(message, "content", "")),
    }


def _content_snapshot(content: object) -> object:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    return [_block_snapshot(block) for block in content]


def _block_snapshot(block: object) -> dict[str, Any]:
    if isinstance(block, Mapping):
        source = cast(Mapping[str, Any], block)
        data_value = source.get("data")
        result: dict[str, Any] = {
            key: value for key, value in source.items() if key != "data" and value not in (None, "")
        }
    else:
        data_value = getattr(block, "data", None)
        result: dict[str, Any] = {}
        for key in ["type", "text", "tool_use_id", "name", "input", "content", "is_error", "media_type"]:
            value = getattr(block, key, None)
            if value not in (None, ""):
                result[key] = value
    if data_value:
        result["data"] = _("<omitted {count} chars>").format(count=len(str(data_value)))
    return result


def _split_system_prompt(system_prompt: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    zone = "static"
    title = _("Preamble")
    lines: list[str] = []

    def flush() -> None:
        content = "\n".join(lines).strip()
        if content:
            sections.append({"title": title, "content": content, "zone": zone})

    for line in system_prompt.splitlines():
        if line.strip() == DYNAMIC_BOUNDARY:
            flush()
            lines = []
            zone = "dynamic"
            title = _("Dynamic Prompt")
            continue
        if line.startswith("# "):
            flush()
            title = line[2:].strip() or _("Section")
            lines = [line]
            continue
        lines.append(line)
    flush()
    return sections


def _metadata_item(label: str, value: str) -> str:
    return (
        '<div class="meta-item"><div class="meta-label">{label}</div><div class="meta-value">{value}</div></div>'
    ).format(label=escape(label), value=escape(value))


def _content_card(title: str, content: str, *, badge: str = "", collapsed: bool = False) -> str:
    tag = "details" if collapsed else "section"
    open_attr = "" if collapsed else ""
    if collapsed:
        header = "<summary>{}</summary>".format(_card_header(title, badge))
    else:
        header = _card_header(title, badge)
    return '<{tag} class="card" {open_attr}>{header}<pre>{content}</pre></{tag}>'.format(
        tag=tag,
        open_attr=open_attr,
        header=header,
        content=escape(content),
    )


def _tab_button(tab_id: str, label: str, *, selected: bool = False) -> str:
    return (
        '<button class="tab-button" type="button" role="tab" '
        'aria-selected="{selected}" data-tab-target="{tab_id}">{label}</button>'
    ).format(
        selected="true" if selected else "false",
        tab_id=escape(tab_id),
        label=escape(label),
    )


def _tab_panel(tab_id: str, content: str, *, active: bool = False) -> str:
    classes = "tab-panel active" if active else "tab-panel"
    return '<section class="{classes}" role="tabpanel" data-tab-panel="{tab_id}">{content}</section>'.format(
        classes=classes,
        tab_id=escape(tab_id),
        content=content,
    )


def _render_all_tab(snapshot: dict[str, Any]) -> str:
    metadata = snapshot.get("metadata") or {}
    system_sections = list(snapshot.get("system_sections") or [])
    provider_messages = list(snapshot.get("provider_messages") or [])
    tools = list(snapshot.get("tools") or [])
    has_recalled_memory = any(_is_recalled_memory_content(message.get("content", "")) for message in provider_messages)
    recalled_line = (
        _("Present in Provider Messages as a hidden conversation <system-reminder>.")
        if has_recalled_memory
        else _("Not present in this snapshot.")
    )
    assembly = "\n".join(
        [
            _("Source: {source}").format(source=metadata.get("source") or _("Current runtime state")),
            "",
            _("1. System Prompt"),
            _("   Provider field: system"),
            _("   Details: System Prompt tab"),
            _("   Sections: {count}").format(count=len(system_sections)),
            "",
            _("2. Provider Messages"),
            _("   Provider field: messages"),
            _("   Details: Provider Messages tab"),
            _("   Messages: {count}").format(count=len(provider_messages)),
            _("   Recalled memory: {status}").format(status=recalled_line),
            "",
            _("3. Tools"),
            _("   Provider field: tools"),
            _("   Details: Tools tab"),
            _("   Tools: {count}").format(count=len(tools)),
        ]
    )
    return "{steps}{summary}".format(
        steps=(
            '<div class="assembly-list">'
            + _assembly_step(
                _("1. System Prompt"),
                "system",
                _("System Prompt"),
                _("Provider system parameter. This is sent before provider messages."),
                _("{count} sections").format(count=len(system_sections)),
            )
            + _assembly_step(
                _("2. Provider Messages"),
                "messages",
                _("Provider Messages"),
                _("Conversation messages in send order. Hidden conversation recalled memory appears here."),
                _("{count} messages; recalled memory {status}").format(
                    count=len(provider_messages),
                    status=_("present") if has_recalled_memory else _("not present"),
                ),
            )
            + _assembly_step(
                _("3. Tools"),
                "tools",
                _("Tools"),
                _("Tool definitions available to the main model for this request."),
                _("{count} tools").format(count=len(tools)),
            )
            + "</div>"
        ),
        summary=_content_card(_("Prompt Assembly Order"), assembly),
    )


def _assembly_step(title: str, tab_id: str, tab_label: str, body: str, meta: str) -> str:
    return (
        '<section class="assembly-step">'
        '<div class="assembly-step-title">{title}</div>'
        '<div class="assembly-step-body">{body} '
        '<button class="inline-tab-link" type="button" data-open-tab="{tab_id}">{button_label}</button>'
        "</div>"
        '<div class="assembly-step-meta">{meta}</div>'
        "</section>"
    ).format(
        title=escape(title),
        body=escape(body),
        tab_id=escape(tab_id),
        button_label=escape(_("Open {tab_label}").format(tab_label=tab_label)),
        meta=escape(meta),
    )


def _message_card(index: int, message: dict[str, Any]) -> str:
    role = str(message.get("role") or _("message"))
    content = message.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, indent=2, ensure_ascii=False)
    badge = _("recalled memory") if _is_recalled_memory_content(message.get("content", "")) else _("message")
    return _content_card("#{index} {role}".format(index=index, role=role), content, badge=badge)


def _is_recalled_memory_content(content: object) -> bool:
    if isinstance(content, str):
        return RECALLED_MEMORY_MARKER in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                block_map = cast(Mapping[str, Any], block)
                if _is_recalled_memory_content(block_map.get("text") or block_map.get("content") or ""):
                    return True
            elif _is_recalled_memory_content(getattr(block, "text", None) or getattr(block, "content", None) or ""):
                return True
    return False


def _tool_card(tool: dict[str, Any]) -> str:
    content = "{description}\n\n{schema_label}:\n{schema}".format(
        description=tool.get("description", ""),
        schema_label=_("Input schema"),
        schema=json.dumps(tool.get("input_schema") or {}, indent=2, ensure_ascii=False),
    )
    return _content_card(str(tool.get("name") or _("tool")), content, badge=_("tool"))


def _card_header(title: str, badge: str = "") -> str:
    badge_html = '<span class="badge">{}</span>'.format(escape(badge)) if badge else ""
    return '<div class="card-header"><span>{title}</span>{badge}</div>'.format(
        title=escape(title),
        badge=badge_html,
    )


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip())
    return cleaned[:80] or "session"


def _prompt_html_path(repl: object) -> Path:
    session_storage = getattr(repl, "_session_storage", None)
    cwd = str(getattr(repl, "_original_cwd", "") or Path.cwd())
    session_id = str(getattr(repl, "_session_id", "") or "current")
    session_dir = getattr(session_storage, "session_dir", None)
    if callable(session_dir):
        try:
            raw_path = session_dir(cwd, session_id)
        except Exception:
            raw_path = None
        if isinstance(raw_path, (str, Path)):
            return Path(raw_path) / "prompt.html"
    return Path(tempfile.gettempdir()) / "iac-code-prompts" / _safe_filename(session_id) / "prompt.html"


def _open_prompt_export(path: Path, browser_opener: object = None) -> None:
    if callable(browser_opener):
        opener = cast(Any, browser_opener)
        opened = opener(path.resolve().as_uri())
        if not opened:
            raise RuntimeError(_("browser opener returned false"))
        return
    _open_path(path)


def _open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=True)
        return
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    subprocess.run(["xdg-open", str(path)], check=True)
