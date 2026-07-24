"""Translate A2A pipeline event envelopes into web transcript events and rows.

The pipeline executor emits fine-grained envelopes (see ``a2a/pipeline_events.py``
and ``a2a/pipeline_stream.py``): ``step_started`` / ``text_delta`` /
``tool_result`` and friends, each carrying stable ``step`` / ``candidate`` scope
identifiers and a global ``sequence``. The web REPL needs that same data in two
shapes:

* **Live** — forwarded while a pipeline turn runs, using the browser's normal SSE
  vocabulary (``pipeline.step.marker`` + ``assistant.message.*`` + ``tool.*``) so
  the main transcript streams char-by-char exactly like normal chat mode.
* **Reload** — folded into stored transcript rows (the schema
  ``SessionManager.load_visible_transcript`` returns) so a refreshed page rebuilds
  the same recovery-style bubbles.

``PipelineTranscriptTranslator`` is the single source of truth for the live
vocabulary. ``build_pipeline_transcript_rows`` folds that same vocabulary into
stored-row specs, guaranteeing the live and reload views stay identical.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from iac_code.i18n import _
from iac_code.pipeline.display_names import display_step_name

# Web SSE event type emitted for pipeline step / candidate / sub-step boundaries.
PIPELINE_MARKER_EVENT = "pipeline.step.marker"
PIPELINE_CONTEXT_EVENT = "pipeline.step.context"

_SUMMARY_LIMIT = 200


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_created_at(value: Any) -> datetime | None:
    """Parse an envelope ``createdAt`` timestamp (``...Z`` ISO-8601)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _duration_between(start: Any, end: Any) -> float | None:
    """Seconds between two ``createdAt`` timestamps, or ``None`` if unparseable
    or negative. Used as a fallback when an envelope omits ``durationS``."""
    start_dt = _parse_created_at(start)
    end_dt = _parse_created_at(end)
    if start_dt is None or end_dt is None:
        return None
    delta = (end_dt - start_dt).total_seconds()
    return delta if delta >= 0 else None


def _result_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_input_text(value: Any) -> str:
    """Serialize a tool's recorded input into the JSON string the web tool card
    expects (``tool.input.delta`` streams a string, mirroring normal chat)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _summarize(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if len(first_line) > _SUMMARY_LIMIT:
        return first_line[:_SUMMARY_LIMIT] + "…"
    return first_line


class PipelineTranscriptTranslator:
    """Stateful fold from A2A pipeline envelopes to web SSE events.

    Envelopes must be pushed in ``sequence`` order (the order they were journaled
    / enqueued). ``candidate_step`` scoped ``text_delta`` / ``tool_result``
    envelopes do not carry their own sub-step id, so the translator tracks the
    active sub-step per candidate from the most recent ``candidate_step_started``.
    """

    def __init__(self) -> None:
        # Content message ids that have already emitted ``assistant.message.start``.
        self._started: set[str] = set()
        # candidate.runId -> (candidate-step run id, step id, attempt, group id).
        # Older journals omit ``candidateStep`` from display envelopes, so the
        # active coordinate remains the compatibility fallback for those rows.
        self._current_sub_step: dict[str, tuple[str, str, int, str]] = {}
        # Content-scope base id -> current segment index. A step/sub-step scope is
        # split into ordered segments so text and tools interleave the way normal
        # chat does (each segment is its own assistant message ordered by
        # sequence). A new segment opens the first time text follows a tool.
        self._segment_index: dict[str, int] = {}
        # Content-scope base id -> whether the current segment already holds a tool.
        self._segment_has_tool: dict[str, bool] = {}
        # group_id -> createdAt of the *_started envelope, so a *_completed
        # envelope missing ``durationS`` can derive the elapsed time.
        self._group_started_at: dict[str, str] = {}
        # toolUseId -> message id assigned when the tool announced ``tool_started``,
        # so the later ``tool_result`` reuses the same segment instead of
        # re-opening one (and re-emitting an already-sent ``tool.started``).
        self._tool_message_id: dict[str, str] = {}
        # Provider message id -> every Web transcript segment opened while that
        # provider message was active. One streamed response can contain
        # text -> tool -> text and therefore span multiple Web messages.
        self._provider_message_segments: dict[str, list[str]] = {}
        # Content-scope base id -> current provider message id.
        self._active_provider_message: dict[str, str] = {}
        # Guard so the "↪ 普通对话" boundary is emitted at most once even if the
        # handoff envelope is replayed/re-pushed.
        self._normal_chat_marker_emitted = False
        # group_id -> {"marker": <working marker event>, "base": <content base id>}
        # for every step/candidate/sub-step that started but has not yet completed.
        # On ``pipeline_canceled`` these are re-emitted as ``canceled`` so a step
        # interrupted mid-run (e.g. deploying) stops showing "进行中" forever.
        self._active_markers: dict[str, dict[str, Any]] = {}
        # group_id -> latest *真实状态* marker event (working/completed) for every
        # step/candidate/sub-step. Used by ``input_required`` to re-emit that group
        # as ``status="input"`` (keep the step expanded while awaiting the user) and
        # by ``input_received`` to restore its prior status. Unlike
        # ``_active_markers`` this is NOT cleared on completion — a step may emit
        # ``step_completed`` and *then* ``input_required`` (e.g. confirm_and_select
        # computes options, marks itself done, then asks the user to pick), so the
        # restore target must survive completion.
        self._group_markers: dict[str, dict[str, Any]] = {}
        # Ordered content message ids of every ``input_required`` prompt bubble
        # (confirm_and_select / ask_user_question ...). The reload path uses this
        # to weave the user's mid-pipeline answer *right after* the prompt it
        # answered — otherwise those replies (persisted separately as
        # ``source=pipeline`` web messages) get appended after the whole replay
        # and appear misordered at the very end (Issue 2).
        self.input_prompt_message_ids: list[str] = []
        # request_id -> {question, options, allowFreeText, toolUseId, messageId}
        # captured at an ask_user_question ``input_required``. The run's journal
        # never emits tool_started/tool_result for ask_user_question (only
        # input_required/input_received), so the matching ``input_received`` uses
        # this to synthesize a completed tool card (question + chosen option),
        # keeping the tool call visible after the interactive panel resolves away.
        self._ask_questions: dict[str, dict[str, Any]] = {}

    # -- public API -----------------------------------------------------------

    def push(self, envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
        env = _as_mapping(envelope)
        if env.get("visibility") == "pending_backup":
            return []
        event_type = str(env.get("eventType") or "")
        handler = getattr(self, f"_on_{event_type}", None)
        if handler is None:
            return []
        return handler(env)

    def translate_all(self, envelopes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for envelope in envelopes:
            out.extend(self.push(envelope))
        return out

    # -- scope helpers --------------------------------------------------------

    def _candidate_step_identity(self, env: Mapping[str, Any]) -> tuple[str, str, int, str]:
        candidate = _as_mapping(env.get("candidate"))
        candidate_step = _as_mapping(env.get("candidateStep"))
        data = _as_mapping(env.get("data"))
        cand_run = str(candidate.get("runId") or "")
        run_id = str(candidate_step.get("runId") or "")
        step_id = str(candidate_step.get("id") or data.get("stepId") or "")
        attempt = _optional_int(candidate_step.get("attempt")) or 1
        if run_id:
            return run_id, step_id, attempt, f"candidate-step:{run_id}"
        current = self._current_sub_step.get(cand_run)
        if current is not None and (not step_id or current[1] == step_id):
            return current
        # Compatibility with journals written before candidateStep coordinates
        # were attached to every candidate-scoped display envelope.
        legacy_run_id = f"{cand_run}-{step_id}" if cand_run and step_id else ""
        legacy_group_id = f"{cand_run}:{step_id}" if cand_run and step_id else ""
        return legacy_run_id, step_id, attempt, legacy_group_id

    def _candidate_step_progress(self, env: Mapping[str, Any]) -> tuple[int | None, int | None]:
        """Return the ``(index, total)`` progress coordinate for a candidate step.

        Candidate steps carry the same ``index``/``total`` as top-level steps (via
        the ``candidateStep`` coordinate), so sub-steps can render the same
        ``(N/M)`` progress suffix (e.g. ``1/3``) instead of a bare title.
        """
        candidate_step = _as_mapping(env.get("candidateStep"))
        return _optional_int(candidate_step.get("index")), _optional_int(candidate_step.get("total"))

    def _content_base_id(self, env: Mapping[str, Any]) -> str:
        scope = str(env.get("scope") or "")
        if scope == "candidate_step":
            run_id, _step_id, _attempt, _group_id = self._candidate_step_identity(env)
            return f"pl-{run_id}" if run_id else ""
        step = _as_mapping(env.get("step"))
        return "pl-{}".format(step.get("runId") or step.get("id") or "")

    def _segment_message_id(self, base: str) -> str:
        """Message id for the *current* segment of a content scope.

        Segment 0 reuses the bare base id (so single-segment scopes keep the
        exact stable id the reload path dedups against); later segments append
        ``#{n}``. ``#`` never appears in run/step identifiers, so segmented ids
        can't collide with another scope's base id.
        """
        if not base:
            return ""
        index = self._segment_index.get(base, 0)
        return base if index == 0 else f"{base}#{index}"

    def _advance_segment(self, base: str) -> None:
        self._segment_index[base] = self._segment_index.get(base, 0) + 1
        self._segment_has_tool[base] = False

    def _text_message_id(self, env: Mapping[str, Any]) -> str:
        """Resolve the message id for streamed text, opening a new segment when
        the current one already carries a tool so text→tool→text interleaves."""
        base = self._content_base_id(env)
        if not base:
            return ""
        if self._segment_has_tool.get(base):
            self._advance_segment(base)
        return self._segment_message_id(base)

    def _ensure_started(self, message_id: str) -> list[dict[str, Any]]:
        if not message_id or message_id in self._started:
            return []
        self._started.add(message_id)
        base = message_id.split("#", 1)[0]
        provider_message_id = self._active_provider_message.get(base)
        if provider_message_id:
            segments = self._provider_message_segments.setdefault(provider_message_id, [])
            if message_id not in segments:
                segments.append(message_id)
        return [{"type": "assistant.message.start", "payload": {"messageId": message_id}}]

    def _end_content_scope(self, base: str) -> list[dict[str, Any]]:
        """End every segment message opened for a content scope."""
        if not base:
            return []
        events: list[dict[str, Any]] = []
        for index in range(self._segment_index.get(base, 0) + 1):
            message_id = base if index == 0 else f"{base}#{index}"
            if message_id in self._started:
                events.append({"type": "assistant.message.end", "payload": {"messageId": message_id}})
        return events

    def _record_group_start(self, group_id: str, env: Mapping[str, Any]) -> None:
        created_at = env.get("createdAt")
        if isinstance(created_at, str) and created_at:
            self._group_started_at[group_id] = created_at

    def _duration_for(self, group_id: str, env: Mapping[str, Any]) -> float | None:
        return _duration_between(self._group_started_at.get(group_id), env.get("createdAt"))

    def _register_active_marker(self, group_id: str, marker: dict[str, Any], base: str) -> None:
        """Track a started-but-not-completed marker so ``pipeline_canceled`` can
        finalize it. ``base`` is the content scope id whose segments to close."""
        self._active_markers[group_id] = {"marker": marker, "base": base}

    def _complete_active_marker(self, group_id: str) -> None:
        self._active_markers.pop(group_id, None)

    def _finalize_active_markers(
        self,
        group_ids: Iterable[str],
        status: str,
        env: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Close active transcript groups with a terminal status.

        Failure envelopes use the same stable marker ids as their corresponding
        start envelopes, so live rendering and journal replay update rows in place.
        """
        events: list[dict[str, Any]] = []
        for group_id in group_ids:
            record = self._active_markers.pop(group_id, None)
            if record is None:
                continue
            base = str(record.get("base") or "")
            if base:
                events.extend(self._end_content_scope(base))
            marker = copy.deepcopy(record.get("marker") or {})
            payload = marker.get("payload")
            if not isinstance(payload, dict):
                continue
            pipeline_step = payload.get("pipelineStep")
            if not isinstance(pipeline_step, dict):
                continue
            pipeline_step["status"] = status
            duration_s = _optional_float(_as_mapping(env.get("data")).get("durationS"))
            if duration_s is None or duration_s <= 0:
                duration_s = self._duration_for(group_id, env)
            if duration_s is not None:
                pipeline_step["durationS"] = duration_s
            self._record_group_marker(group_id, marker)
            events.append(marker)
        return events

    def _record_group_marker(self, group_id: str, marker: dict[str, Any]) -> None:
        """Remember a group's latest real-status marker so a later
        ``input_required`` can re-emit it as ``status="input"`` and
        ``input_received`` can restore it."""
        if group_id:
            self._group_markers[group_id] = marker

    def _group_id_for(self, env: Mapping[str, Any]) -> str:
        """Resolve the marker group id an ``input_required``/``input_received``
        envelope belongs to, matching the id its *_started handler registered."""
        scope = str(env.get("scope") or "")
        if scope == "candidate_step":
            _run_id, _step_id, _attempt, group_id = self._candidate_step_identity(env)
            return group_id
        if scope == "candidate":
            candidate = _as_mapping(env.get("candidate"))
            cand_run = str(candidate.get("runId") or "")
            return f"candidate:{cand_run}" if cand_run else ""
        # ``step`` scope, and parent-scoped ask_user_question (scope falls back to
        # "step" whenever a parent step is active, e.g. intent_parsing / step1).
        step = _as_mapping(env.get("step"))
        run_id = str(step.get("runId") or step.get("id") or "")
        return f"step:{run_id}" if run_id else ""

    def _input_marker_events(self, env: Mapping[str, Any], status: str) -> list[dict[str, Any]]:
        """Re-emit the marker for the scope an input event targets, cloned with a
        new ``status``. ``status="input"`` keeps the step expanded while awaiting
        the user; restoring the stored marker (its real status) collapses it once
        input arrives. The clone keeps the SAME ``markerId`` so the frontend
        updates the existing group in place instead of adding a duplicate."""
        group_id = self._group_id_for(env)
        base = self._group_markers.get(group_id)
        if not base:
            return []
        if status == "":
            # Restore: re-emit the stored marker verbatim (its real status).
            return [copy.deepcopy(base)]
        clone = copy.deepcopy(base)
        step = clone.get("payload", {}).get("pipelineStep")
        if isinstance(step, dict):
            step["status"] = status
        return [clone]

    def _marker_event(
        self,
        *,
        kind: str,
        level: str,
        marker_id: str,
        content: str,
        step_id: str,
        title: str,
        index: int | None,
        total: int | None,
        status: str,
        depth: int,
        group_id: str,
        parent_group_id: str,
        parent_step_id: str = "",
        candidate_name: str = "",
        attempt_no: int = 1,
        duration_s: float | None = None,
        outcome: str = "",
    ) -> dict[str, Any]:
        return {
            "type": PIPELINE_MARKER_EVENT,
            "payload": {
                "markerId": marker_id,
                "kind": kind,
                "content": content,
                "pipelineStep": {
                    "level": level,
                    "stepId": step_id,
                    "title": title,
                    "index": index,
                    "total": total,
                    "status": status,
                    "attemptNo": attempt_no,
                    "parentStepId": parent_step_id,
                    "candidateName": candidate_name,
                    "groupId": group_id,
                    "parentGroupId": parent_group_id,
                    "depth": depth,
                    "durationS": duration_s,
                    # 终态枚举(completed/failed/canceled/early_exit)。仅 pipeline_outcome
                    # marker 会带,前端据此决定彩条颜色/图标/中文文案。
                    "outcome": outcome,
                },
            },
        }

    # -- event handlers -------------------------------------------------------

    def _on_context_usage(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Live per-step context-window usage. Keyed by the SAME group id as the
        scope's markers so the frontend can attach the ring to the active step and
        drop it when the step's marker reaches a terminal status."""
        group_id = self._group_id_for(env)
        if not group_id:
            return []
        scope = str(env.get("scope") or "")
        data = _as_mapping(env.get("data"))
        candidate = _as_mapping(env.get("candidate"))
        if scope == "candidate_step":
            _run_id, step_id, attempt, _gid = self._candidate_step_identity(env)
            level = "sub_step"
            title = display_step_name(step_id)
            candidate_name = str(candidate.get("name") or "")
        elif scope == "candidate":
            level = "candidate"
            step_id = str(candidate.get("id") or "")
            candidate_name = str(candidate.get("name") or "")
            title = candidate_name
            attempt = _optional_int(candidate.get("attempt")) or 1
        else:
            step = _as_mapping(env.get("step"))
            step_id = str(step.get("id") or "")
            level = "step"
            title = display_step_name(step_id)
            candidate_name = ""
            attempt = _optional_int(step.get("attempt")) or 1
        return [
            {
                "type": PIPELINE_CONTEXT_EVENT,
                "payload": {
                    "groupId": group_id,
                    "level": level,
                    "stepId": step_id,
                    "title": title,
                    "candidateName": candidate_name,
                    "attemptNo": attempt,
                    "contextUsage": dict(data),
                },
            }
        ]

    def _on_step_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        step = _as_mapping(env.get("step"))
        run_id = str(step.get("runId") or step.get("id") or "")
        step_id = str(step.get("id") or "")
        title = display_step_name(step_id)
        index = _optional_int(step.get("index"))
        total = _optional_int(step.get("total"))
        content = "● {}".format(title or step_id or "Step")
        if index is not None and total is not None:
            content += " ({}/{})".format(index, total)
        self._record_group_start(f"step:{run_id}", env)
        marker = self._marker_event(
            kind="pipeline_step",
            level="step",
            marker_id=f"plmk-{run_id}",
            content=content,
            step_id=step_id,
            title=title,
            index=index,
            total=total,
            status="working",
            depth=0,
            group_id=f"step:{run_id}",
            parent_group_id="",
        )
        self._register_active_marker(f"step:{run_id}", marker, f"pl-{run_id}")
        self._record_group_marker(f"step:{run_id}", marker)
        return [marker]

    def _on_step_completed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        step = _as_mapping(env.get("step"))
        run_id = str(step.get("runId") or step.get("id") or "")
        step_id = str(step.get("id") or "")
        title = display_step_name(step_id)
        index = _optional_int(step.get("index"))
        total = _optional_int(step.get("total"))
        content = "● {}".format(title or step_id or "Step")
        if index is not None and total is not None:
            content += " ({}/{})".format(index, total)
        duration_s = _optional_float(_as_mapping(env.get("data")).get("durationS"))
        # ``durationS`` of 0 (not just missing) means the executor did not record a
        # real elapsed time; fall back to the createdAt span so e.g. step 3 shows
        # its true duration instead of nothing.
        if duration_s is None or duration_s <= 0:
            duration_s = self._duration_for(f"step:{run_id}", env)
        self._complete_active_marker(f"step:{run_id}")
        events = self._end_content_scope(f"pl-{run_id}")
        marker = self._marker_event(
            kind="pipeline_step",
            level="step",
            marker_id=f"plmk-{run_id}",
            content=content,
            step_id=step_id,
            title=title,
            index=index,
            total=total,
            status="completed",
            depth=0,
            group_id=f"step:{run_id}",
            parent_group_id="",
            duration_s=duration_s,
        )
        self._record_group_marker(f"step:{run_id}", marker)
        events.append(marker)
        return events

    def _on_step_failed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        step = _as_mapping(env.get("step"))
        run_id = str(step.get("runId") or step.get("id") or "")
        return self._finalize_active_markers([f"step:{run_id}"], "failed", env)

    def _on_candidate_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidate = _as_mapping(env.get("candidate"))
        step = _as_mapping(env.get("step"))
        data = _as_mapping(env.get("data"))
        cand_run = str(candidate.get("runId") or "")
        step_run = str(step.get("runId") or step.get("id") or "")
        name = str(candidate.get("name") or data.get("candidateName") or "")
        self._record_group_start(f"candidate:{cand_run}", env)
        marker = self._marker_event(
            kind="pipeline_candidate",
            level="candidate",
            marker_id=f"plmk-{cand_run}",
            content=_("◆ Plan: {}").format(name or "Candidate"),
            step_id=str(candidate.get("id") or ""),
            title=name,
            index=None,
            total=None,
            status="working",
            depth=1,
            group_id=f"candidate:{cand_run}",
            parent_group_id=f"step:{step_run}",
            parent_step_id=str(step.get("id") or ""),
            candidate_name=name,
        )
        self._register_active_marker(f"candidate:{cand_run}", marker, "")
        self._record_group_marker(f"candidate:{cand_run}", marker)
        return [marker]

    def _on_candidate_completed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidate = _as_mapping(env.get("candidate"))
        step = _as_mapping(env.get("step"))
        data = _as_mapping(env.get("data"))
        cand_run = str(candidate.get("runId") or "")
        step_run = str(step.get("runId") or step.get("id") or "")
        name = str(candidate.get("name") or data.get("candidateName") or "")
        duration_s = _optional_float(data.get("durationS"))
        if duration_s is None or duration_s <= 0:
            duration_s = self._duration_for(f"candidate:{cand_run}", env)
        self._complete_active_marker(f"candidate:{cand_run}")
        marker = self._marker_event(
            kind="pipeline_candidate",
            level="candidate",
            marker_id=f"plmk-{cand_run}",
            content=_("◆ Plan: {}").format(name or "Candidate"),
            step_id=str(candidate.get("id") or ""),
            title=name,
            index=None,
            total=None,
            status="completed",
            depth=1,
            group_id=f"candidate:{cand_run}",
            parent_group_id=f"step:{step_run}",
            parent_step_id=str(step.get("id") or ""),
            candidate_name=name,
            duration_s=duration_s,
        )
        self._record_group_marker(f"candidate:{cand_run}", marker)
        return [marker]

    def _on_candidate_failed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidate = _as_mapping(env.get("candidate"))
        cand_run = str(candidate.get("runId") or "")
        return self._finalize_active_markers([f"candidate:{cand_run}"], "failed", env)

    def _on_candidate_step_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidate = _as_mapping(env.get("candidate"))
        cand_run = str(candidate.get("runId") or "")
        run_id, step_id, attempt, group_id = self._candidate_step_identity(env)
        self._current_sub_step[cand_run] = (run_id, step_id, attempt, group_id)
        data = _as_mapping(env.get("data"))
        title = display_step_name(step_id)
        candidate_name = str(candidate.get("name") or data.get("candidateName") or "")
        index, total = self._candidate_step_progress(env)
        content = "· {}".format(title or step_id or "Step")
        if index is not None and total is not None:
            content += " ({}/{})".format(index, total)
        self._record_group_start(group_id, env)
        marker = self._marker_event(
            kind="pipeline_sub_step",
            level="sub_step",
            marker_id=f"plmk-{run_id}",
            content=content,
            step_id=step_id,
            title=title,
            index=index,
            total=total,
            status="working",
            depth=2,
            group_id=group_id,
            parent_group_id=f"candidate:{cand_run}",
            parent_step_id=str(candidate.get("id") or ""),
            candidate_name=candidate_name,
            attempt_no=attempt,
        )
        self._register_active_marker(group_id, marker, f"pl-{run_id}")
        self._record_group_marker(group_id, marker)
        return [marker]

    def _on_candidate_step_completed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        candidate = _as_mapping(env.get("candidate"))
        data = _as_mapping(env.get("data"))
        cand_run = str(candidate.get("runId") or "")
        run_id, step_id, attempt, group_id = self._candidate_step_identity(env)
        title = display_step_name(step_id)
        candidate_name = str(candidate.get("name") or data.get("candidateName") or "")
        duration_s = _optional_float(data.get("durationS"))
        if duration_s is None or duration_s <= 0:
            duration_s = self._duration_for(group_id, env)
        index, total = self._candidate_step_progress(env)
        content = "· {}".format(title or step_id or "Step")
        if index is not None and total is not None:
            content += " ({}/{})".format(index, total)
        self._complete_active_marker(group_id)
        events = self._end_content_scope(f"pl-{run_id}")
        marker = self._marker_event(
            kind="pipeline_sub_step",
            level="sub_step",
            marker_id=f"plmk-{run_id}",
            content=content,
            step_id=step_id,
            title=title,
            index=index,
            total=total,
            status="completed",
            depth=2,
            group_id=group_id,
            parent_group_id=f"candidate:{cand_run}",
            parent_step_id=str(candidate.get("id") or ""),
            candidate_name=candidate_name,
            attempt_no=attempt,
            duration_s=duration_s,
        )
        self._record_group_marker(group_id, marker)
        events.append(marker)
        return events

    def _on_candidate_step_failed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        _run_id, _step_id, _attempt, group_id = self._candidate_step_identity(env)
        return self._finalize_active_markers([group_id], "failed", env)

    def _on_context_compaction_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        # 流水线子代理里触发的自动压缩:折成与普通模式同款的 compaction.started SSE,把「正在自动压缩
        # 上下文」流光条(buildCompactionIndicator)拉起来。pipeline_events 现会转发 started/failed 相位
        # (旧实现只放行 finished),这里补齐运行态的「起」。带上 groupId(与结束态边界条同源经
        # _group_id_for 解析),让前端把运行态压缩条精确挂进触发压缩的那个步骤/候选组——并行候选阶段
        # 单凭「首个进行中叶子」会错挂(如方案2压缩却显示在方案1)。
        group_id = self._group_id_for(env)
        payload = {"auto": True, "state": "started", "available": True, "groupId": group_id}
        return [{"type": "compaction.started", "payload": payload}]

    def _on_context_compaction_failed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        # 压缩失败:撤掉运行态压缩条并给一次性失败提示(buildCompactionNotice 的 failed 分支)。
        return [{"type": "compaction.finished", "payload": {"auto": True, "state": "failed"}}]

    def _on_context_compacted(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        data = _as_mapping(env.get("data"))
        summary = str(data.get("summary") or "")
        group_id = self._group_id_for(env)
        base = self._group_markers.get(group_id)
        base_step = _as_mapping(base.get("payload", {}).get("pipelineStep")) if base else {}
        marker_id = f"plmk-compact-{env.get('eventId') or ''}"
        # 先撤运行态压缩条(compaction.finished),再落持久分隔条。不带 state="success",避免命中
        # app.js 仅对手动压缩成功触发的整会话重载路径(自动压缩不应打断流式)。
        return [
            {
                "type": "compaction.finished",
                "payload": {
                    "auto": True,
                    "originalTokens": data.get("originalTokens"),
                    "compactedTokens": data.get("compactedTokens"),
                },
            },
            self._marker_event(
                kind="context_compaction_boundary",
                level=str(base_step.get("level") or ""),
                marker_id=marker_id,
                content=summary,
                step_id=str(base_step.get("stepId") or ""),
                title=str(base_step.get("title") or ""),
                index=base_step.get("index"),
                total=base_step.get("total"),
                status="completed",
                depth=int(base_step.get("depth") or 0),
                group_id=group_id,
                parent_group_id=str(base_step.get("parentGroupId") or ""),
                parent_step_id=str(base_step.get("parentStepId") or ""),
                candidate_name=str(base_step.get("candidateName") or ""),
            )
        ]

    def _on_text_delta(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        message_id = self._text_message_id(env)
        if not message_id:
            return []
        text = str(_as_mapping(env.get("data")).get("text") or "")
        if not text:
            return []
        events = self._ensure_started(message_id)
        events.append({"type": "assistant.text.delta", "payload": {"messageId": message_id, "delta": text}})
        return events

    def _on_thinking_delta(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        # Thinking precedes text within a turn and carries no tool yet, so it resolves to the
        # same segment as the following text (segment advance only fires once a tool lands) —
        # mirroring normal mode where one message holds both thinking and content. The frontend
        # reducer folds assistant.thinking.delta into message.thinking, driving 正在思考/思考完成.
        message_id = self._text_message_id(env)
        if not message_id:
            return []
        text = str(_as_mapping(env.get("data")).get("text") or "")
        if not text:
            return []
        events = self._ensure_started(message_id)
        events.append({"type": "assistant.thinking.delta", "payload": {"messageId": message_id, "delta": text}})
        return events

    def _on_message_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        provider_message_id = str(_as_mapping(env.get("data")).get("messageId") or "")
        base = self._content_base_id(env)
        if provider_message_id and base:
            self._active_provider_message[base] = provider_message_id
            self._provider_message_segments.setdefault(provider_message_id, [])
        return []

    def _on_message_tombstone(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        data = _as_mapping(env.get("data"))
        provider_message_id = str(data.get("messageId") or "")
        affected_tool_use_ids = [
            str(tool_use_id)
            for tool_use_id in data.get("affectedToolUseIds", [])
            if isinstance(tool_use_id, str) and tool_use_id
        ]
        provider_message_known = provider_message_id in self._provider_message_segments
        message_ids = list(self._provider_message_segments.pop(provider_message_id, []))
        mapped_tool_message_ids = [
            self._tool_message_id[tool_use_id]
            for tool_use_id in affected_tool_use_ids
            if tool_use_id in self._tool_message_id
        ]
        if not message_ids and mapped_tool_message_ids:
            # Compatibility for journals created before message_started was
            # persisted: the failed provider response begins at the earliest
            # affected tool segment and may continue through the current segment.
            first = mapped_tool_message_ids[0]
            base, _, suffix = first.partition("#")
            first_index = int(suffix) if suffix.isdigit() else 0
            current_index = self._segment_index.get(base, first_index)
            message_ids = [base if index == 0 else f"{base}#{index}" for index in range(first_index, current_index + 1)]
        if not message_ids and not provider_message_known:
            fallback_message_id = self._segment_message_id(self._content_base_id(env))
            if fallback_message_id:
                message_ids = [fallback_message_id]
        for tool_use_id in affected_tool_use_ids:
            self._tool_message_id.pop(tool_use_id, None)
        if not message_ids and not affected_tool_use_ids:
            return []
        # A fallback provider can reuse the same pipeline scope after retracting a
        # partial response. Let the replacement emit a fresh message.start for the
        # stable segment id instead of appending to a deleted frontend message.
        reset_indices: dict[str, int] = {}
        events: list[dict[str, Any]] = []
        for message_id in message_ids:
            self._started.discard(message_id)
            base, _, suffix = message_id.partition("#")
            index = int(suffix) if suffix.isdigit() else 0
            reset_indices[base] = min(reset_indices.get(base, index), index)
            if self._active_provider_message.get(base) == provider_message_id:
                self._active_provider_message.pop(base, None)
            events.append(
                {
                    "type": "assistant.message.tombstone",
                    "payload": {
                        "messageId": message_id,
                        "affectedToolUseIds": affected_tool_use_ids,
                    },
                }
            )
        for base, index in reset_indices.items():
            self._segment_index[base] = index
            self._segment_has_tool[base] = False
        return events

    def _on_tool_started(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Announce a tool as *running* the moment its call is emitted (before its
        result arrives), so the web card shows ``正在…`` instead of jumping
        straight to ``已…``."""
        data = _as_mapping(env.get("data"))
        tool_use_id = str(data.get("toolUseId") or "")
        if not tool_use_id:
            return []
        # ask_user_question is surfaced as an interactive question panel via its
        # ``input_required`` envelope (see ``_on_input_required``), not as a tool
        # card. Its ``tool_started`` (emitted from ToolUseEndEvent) would otherwise
        # linger cardless while the run pauses and get finalized to 「已取消」.
        if str(data.get("toolName") or "") == "ask_user_question":
            return []
        base = self._content_base_id(env)
        if not base:
            return []
        # The tool attaches to the current segment; a later text delta opens the
        # next segment so the transcript reads text → tool(s) → text → tool(s).
        message_id = self._segment_message_id(base)
        self._segment_has_tool[base] = True
        self._tool_message_id[tool_use_id] = message_id
        tool_name = str(data.get("toolName") or "")
        input_text = _tool_input_text(data.get("input"))
        events = self._ensure_started(message_id)
        events.append(
            {
                "type": "tool.started",
                "payload": {
                    "toolUseId": tool_use_id,
                    "toolName": tool_name,
                    "messageId": message_id,
                    "status": "running",
                },
            }
        )
        if input_text:
            events.append(
                {
                    "type": "tool.input.delta",
                    "payload": {"toolUseId": tool_use_id, "messageId": message_id, "delta": input_text},
                }
            )
        return events

    def _on_tool_result(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        data = _as_mapping(env.get("data"))
        tool_use_id = str(data.get("toolUseId") or "")
        if not tool_use_id:
            return []
        # Mirror ``_on_tool_started``: ask_user_question renders as a question
        # panel, never a tool card, so its result envelope produces nothing.
        if str(data.get("toolName") or "") == "ask_user_question":
            return []
        # Reuse the segment opened by ``tool_started`` so the running card is
        # completed in place. Fall back to opening one when no start was seen
        # (older journals that predate ``tool_started`` envelopes).
        already_started = tool_use_id in self._tool_message_id
        if already_started:
            message_id = self._tool_message_id[tool_use_id]
        else:
            base = self._content_base_id(env)
            if not base:
                return []
            # The tool attaches to the current segment; a later text delta opens
            # the next segment so the transcript reads text → tool(s) → text.
            message_id = self._segment_message_id(base)
            self._segment_has_tool[base] = True
        tool_name = str(data.get("toolName") or "")
        is_error = bool(data.get("isError"))
        result_text = _result_to_text(data.get("result"))
        summary = _summarize(result_text)
        status = "failed" if is_error else "completed"
        input_text = _tool_input_text(data.get("input"))
        events = self._ensure_started(message_id)
        if not already_started:
            events.append(
                {
                    "type": "tool.started",
                    "payload": {
                        "toolUseId": tool_use_id,
                        "toolName": tool_name,
                        "messageId": message_id,
                        "status": "running",
                    },
                }
            )
            if input_text:
                events.append(
                    {
                        "type": "tool.input.delta",
                        "payload": {"toolUseId": tool_use_id, "messageId": message_id, "delta": input_text},
                    }
                )
        events.append(
            {
                "type": "tool.result",
                "payload": {
                    "toolUseId": tool_use_id,
                    "messageId": message_id,
                    "content": result_text,
                    "summary": summary,
                    "resultKind": "text",
                    "isError": is_error,
                    "artifacts": [],
                },
            }
        )
        events.append(
            {
                "type": "tool.finished",
                "payload": {
                    "toolUseId": tool_use_id,
                    "messageId": message_id,
                    "status": status,
                    "summary": summary,
                },
            }
        )
        return events

    def _on_stack_progress(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Fold a stack lifecycle progress frame into an inline ``pipeline.event``.

        The frontend reducer (events.js ``case "pipeline.event"``) attaches the
        frame to ``state.tools[toolUseId].stackProgress`` so the inline step's
        tool card renders the REPL-style resource table + percentage. Same fold
        drives both live streaming and reload.
        """
        return self._stack_progress_event(env, kind="stack.progress")

    def _on_stack_instances_progress(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self._stack_progress_event(env, kind="stack.instances.progress")

    def _stack_progress_event(self, env: Mapping[str, Any], *, kind: str) -> list[dict[str, Any]]:
        data = _as_mapping(env.get("data"))
        tool_use_id = str(data.get("toolUseId") or "")
        if not tool_use_id:
            return []
        payload: dict[str, Any] = {
            "kind": kind,
            "toolUseId": tool_use_id,
            "status": data.get("status"),
            "progressPercentage": data.get("progressPercentage"),
            "elapsedSeconds": data.get("elapsedSeconds"),
        }
        if kind == "stack.progress":
            payload["stackName"] = data.get("stackName")
            payload["stackId"] = data.get("stackId")
            payload["resources"] = data.get("resources")
        else:
            payload["stackGroupName"] = data.get("stackGroupName")
            payload["operationId"] = data.get("operationId")
            payload["instances"] = data.get("instances")
        # Bind to the tool card's message when its ``tool_started`` was already
        # folded, so the progress attaches to the right inline step on reload.
        message_id = self._tool_message_id.get(tool_use_id)
        if message_id:
            payload["messageId"] = message_id
        return [{"type": "pipeline.event", "payload": payload}]

    def _on_input_required(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        data = _as_mapping(env.get("data"))
        prompt = str(data.get("prompt") or "")
        prompt_message_id = ""
        if prompt:
            message_id = self._text_message_id(env)
            if message_id:
                prompt_message_id = message_id
                events.extend(self._ensure_started(message_id))
                events.append({"type": "assistant.text.delta", "payload": {"messageId": message_id, "delta": prompt}})
                # Record this prompt bubble as an anchor so the reload path can
                # slot the user's answer directly after it (Issue 2).
                self.input_prompt_message_ids.append(message_id)
        # ask_user_question additionally surfaces an interactive question panel
        # (options + free text) via the existing question.request → blocking-panel
        # path. confirm_and_select keeps its own inline candidate selector, so this
        # is scoped strictly to ``kind == "ask_user_question"``. Same fold on live
        # and reload; ``_on_input_received`` clears it once answered.
        if str(data.get("kind") or "") == "ask_user_question":
            events.extend(self._ask_user_question_request_event(data))
            self._record_ask_question(data, prompt_message_id)
        # Re-emit the owning step/sub-step marker as ``status="input"`` so the
        # frontend keeps it expanded (with a "等待输入" hint) instead of folding a
        # step that is really waiting on the user. A step may emit ``step_completed``
        # *before* ``input_required`` (confirm_and_select computes its options, marks
        # itself done, then asks the user to pick), so this override is what keeps
        # the selection prompt visible — otherwise the completed step folds and the
        # user thinks the run is stuck. Covers step-scope (confirm_and_select,
        # step1 ask_user_question) and candidate/sub-step scoped questions alike.
        events.extend(self._input_marker_events(env, "input"))
        return events

    def _on_input_received(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        # Input satisfied: restore the group's real status (completed/working) so
        # the step folds back once the user has answered (and so reloading a fully
        # completed run does not leave a step stuck showing "等待输入").
        events: list[dict[str, Any]] = []
        data = _as_mapping(env.get("data"))
        if str(data.get("kind") or "") == "ask_user_question":
            request_id = self._ask_user_question_request_id(data)
            if request_id:
                events.append({"type": "question.resolved", "payload": {"requestId": request_id, "answer": {}}})
            # Render the answered question as a completed tool card so the tool
            # call stays visible once the interactive panel is resolved away.
            events.extend(self._ask_user_question_card_events(env, data, request_id))
        events.extend(self._input_marker_events(env, ""))
        return events

    @staticmethod
    def _ask_user_question_request_id(data: Mapping[str, Any]) -> str:
        """Stable id shared by the input_required/input_received envelopes so the
        question panel is added then removed against the same key (``ask-<id>``)."""
        input_id = str(data.get("inputId") or "")
        if input_id:
            return input_id
        tool_use_id = str(data.get("toolUseId") or "")
        return f"ask-{tool_use_id}" if tool_use_id else ""

    def _record_ask_question(self, data: Mapping[str, Any], message_id: str) -> None:
        """Stash an ask_user_question's question/options (and the prompt bubble's
        message id) so the matching ``input_received`` can synthesize its tool
        card. Keyed by the same stable ``ask-<id>`` used for the panel."""
        request_id = self._ask_user_question_request_id(data)
        if not request_id:
            return
        options = data.get("options")
        self._ask_questions[request_id] = {
            "toolUseId": str(data.get("toolUseId") or ""),
            "question": str(data.get("question") or data.get("prompt") or ""),
            "options": options if isinstance(options, list) else [],
            "allowFreeText": bool(data.get("allowFreeText")),
            "messageId": message_id,
        }

    @staticmethod
    def _ask_answer_text(data: Mapping[str, Any]) -> str:
        """Result body for the ask card: the chosen option's label when a
        structured option was picked. Free-text answers record only a length in
        the journal (the text itself is woven in as the answer bubble), so the
        card's result stays empty rather than echoing a redacted placeholder."""
        return str(data.get("selectedLabel") or "")

    def _ask_user_question_card_events(
        self, env: Mapping[str, Any], data: Mapping[str, Any], request_id: str
    ) -> list[dict[str, Any]]:
        stashed = self._ask_questions.pop(request_id, {})
        tool_use_id = str(data.get("toolUseId") or stashed.get("toolUseId") or "")
        if not tool_use_id:
            return []
        # Attach to the prompt bubble's message when known so it reads as
        # "assistant asked X" followed by the tool card (one assistant message
        # holding text then a tool, like normal chat); otherwise fall back to the
        # current segment of this scope.
        message_id = stashed.get("messageId") or self._segment_message_id(self._content_base_id(env))
        if not message_id:
            return []
        # Prefer the question/options carried on this ``input_received`` envelope
        # (self-contained since the executor echoes them from the input_required
        # it answers). The stash is the fallback for older journals whose
        # input_received omitted them — on reload ``translate_all`` replays the
        # input_required first, so the stash is populated there. It is empty only
        # on the live *resume* run (a fresh translator that never saw the paused
        # run's input_required), which is exactly when the echoed fields matter.
        question = str(data.get("question") or stashed.get("question") or "")
        options = data.get("options")
        if not isinstance(options, list):
            options = stashed.get("options") or []
        allow_free_text = data.get("allowFreeText")
        if not isinstance(allow_free_text, bool):
            allow_free_text = bool(stashed.get("allowFreeText"))
        input_payload: dict[str, Any] = {"question": question}
        if options:
            input_payload["options"] = options
        if allow_free_text:
            input_payload["allowFreeText"] = True
        input_text = _tool_input_text(input_payload)
        answer_text = self._ask_answer_text(data)
        summary = _summarize(answer_text)
        events = self._ensure_started(message_id)
        self._segment_has_tool[message_id.split("#", 1)[0]] = True
        events.append(
            {
                "type": "tool.started",
                "payload": {
                    "toolUseId": tool_use_id,
                    "toolName": "ask_user_question",
                    "messageId": message_id,
                    "status": "running",
                },
            }
        )
        if input_text:
            events.append(
                {
                    "type": "tool.input.delta",
                    "payload": {"toolUseId": tool_use_id, "messageId": message_id, "delta": input_text},
                }
            )
        events.append(
            {
                "type": "tool.result",
                "payload": {
                    "toolUseId": tool_use_id,
                    "messageId": message_id,
                    "content": answer_text,
                    "summary": summary,
                    "resultKind": "text",
                    "isError": False,
                    "artifacts": [],
                },
            }
        )
        events.append(
            {
                "type": "tool.finished",
                "payload": {
                    "toolUseId": tool_use_id,
                    "messageId": message_id,
                    "status": "completed",
                    "summary": summary,
                },
            }
        )
        return events

    def _ask_user_question_request_event(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        request_id = self._ask_user_question_request_id(data)
        if not request_id:
            return []
        options = data.get("options")
        return [
            {
                "type": "question.request",
                "payload": {
                    "requestId": request_id,
                    "payload": {
                        # ``pipeline`` tells the frontend to route the answer back
                        # through the standard pipeline message channel instead of
                        # /api/questions/{id}/answer (which the selling pipeline's
                        # paused question is not registered with).
                        "pipeline": True,
                        "toolUseId": str(data.get("toolUseId") or ""),
                        "question": str(data.get("question") or data.get("prompt") or ""),
                        "options": options if isinstance(options, list) else [],
                        "allowFreeText": bool(data.get("allowFreeText")),
                        "freeTextPrompt": str(data.get("freeTextPrompt") or ""),
                    },
                },
            }
        ]

    def _on_pipeline_handoff_ready(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Emit a single "↪ 普通对话" boundary when the pipeline hands the session
        over to normal chat, so the live main transcript shows the same divider
        the reload path inserts (``append_normal_chat_marker``). Without this the
        live stream jumps straight from the last pipeline step into free-form
        chat with no visual cue that the pipeline is done."""
        data = _as_mapping(env.get("data"))
        if str(data.get("action") or "") != "switch_to_normal":
            return []
        if self._normal_chat_marker_emitted:
            return []
        self._normal_chat_marker_emitted = True
        events: list[dict[str, Any]] = []
        # 先发一条「流水线结局」彩条,序号必早于下面的 boundary,故它落在
        # 「↪ 普通对话」分隔的紧前方——正是「进入普通对话前的最后一条」。
        # 终态枚举随 pipeline_handoff_ready 信封的 ``outcome`` 字段而来
        # (pipeline_executor 里由 terminal_outcome_from_completed_event 计算)。
        outcome = str(data.get("outcome") or "")
        if outcome:
            events.append(
                self._marker_event(
                    kind="pipeline_outcome",
                    level="normal_chat",
                    marker_id="plmk-outcome",
                    content="",
                    step_id="",
                    title="",
                    index=None,
                    total=None,
                    status=outcome,
                    depth=0,
                    group_id="pipeline-outcome",
                    parent_group_id="",
                    outcome=outcome,
                )
            )
        events.append(
            self._marker_event(
                kind="normal_chat_boundary",
                level="normal_chat",
                marker_id="plmk-normal-chat",
                content=_("↪ Normal chat"),
                step_id="",
                title=_("Normal chat"),
                index=None,
                total=None,
                status="",
                depth=0,
                group_id="normal-chat",
                parent_group_id="",
            )
        )
        return events

    def _on_pipeline_canceled(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Finalize every still-running step/candidate/sub-step as ``canceled`` when
        the pipeline is interrupted. Without this an in-flight step (e.g. deploying
        canceled mid-run) keeps its ``working`` status and renders "进行中" forever
        on reload. Re-emitting each marker with the same ``markerId`` updates it in
        place (live) and folds onto the existing stored row (reload)."""
        # Close deepest-first (most recently started) so content scopes end before
        # their parent markers flip; order among markers is irrelevant since each
        # reuses its stable markerId.
        return self._finalize_active_markers(reversed(list(self._active_markers)), "canceled", env)

    def _on_pipeline_failed(self, env: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Fail any group not already closed by a more specific failure event."""
        return self._finalize_active_markers(reversed(list(self._active_markers)), "failed", env)


def build_pipeline_transcript_rows(envelopes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold pipeline envelopes into ordered stored-row specs for reload.

    Each spec matches the keyword arguments of
    ``SessionManager.load_visible_transcript.append_visible_message`` so the
    reload path renders bubbles identical to the live stream. Returns a list of
    ``{"id", "role", "content", "kind", "pipelineStep", "toolUseIds", "tools"}``
    specs in transcript order. ``id`` is the same stable message id the live
    translator uses as its ``ensureMessage`` key (``plmk-*`` for markers,
    ``pl-*`` for content), so a mid-run reload dedups stored rows against the
    replayed live SSE stream instead of duplicating them.
    """

    translator = PipelineTranscriptTranslator()
    events = translator.translate_all(envelopes)

    specs: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}

    for event in events:
        event_type = event.get("type")
        payload = _as_mapping(event.get("payload"))
        if event_type == PIPELINE_MARKER_EVENT:
            marker_id = str(payload.get("markerId") or "")
            pipeline_step = dict(_as_mapping(payload.get("pipelineStep")))
            existing = by_id.get(marker_id)
            if existing is not None:
                existing["pipelineStep"] = pipeline_step
                existing["content"] = str(payload.get("content") or "")
                continue
            spec = {
                "id": marker_id,
                "role": "assistant",
                "content": str(payload.get("content") or ""),
                "kind": str(payload.get("kind") or ""),
                "pipelineStep": pipeline_step,
                "toolUseIds": [],
                "tools": {},
            }
            specs.append(spec)
            by_id[marker_id] = spec
        elif event_type == "assistant.message.start":
            message_id = str(payload.get("messageId") or "")
            if message_id in by_id:
                continue
            spec = {
                "id": message_id,
                "role": "assistant",
                "content": "",
                "thinking": "",
                "kind": "",
                "pipelineStep": None,
                "toolUseIds": [],
                "tools": {},
            }
            specs.append(spec)
            by_id[message_id] = spec
        elif event_type == "assistant.message.tombstone":
            message_id = str(payload.get("messageId") or "")
            removed_spec = by_id.pop(message_id, None)
            affected_tool_ids = {
                str(tool_use_id)
                for tool_use_id in payload.get("affectedToolUseIds", [])
                if isinstance(tool_use_id, str) and tool_use_id
            }
            if removed_spec is not None:
                affected_tool_ids.update(str(tool_use_id) for tool_use_id in removed_spec["toolUseIds"])
                specs = [spec for spec in specs if spec is not removed_spec]
            for tool_use_id in affected_tool_ids:
                tools.pop(tool_use_id, None)
            for spec in specs:
                spec["toolUseIds"] = [
                    tool_use_id for tool_use_id in spec["toolUseIds"] if tool_use_id not in affected_tool_ids
                ]
                for tool_use_id in affected_tool_ids:
                    spec["tools"].pop(tool_use_id, None)
        elif event_type == "assistant.text.delta":
            spec = by_id.get(str(payload.get("messageId") or ""))
            if spec is not None:
                spec["content"] += str(payload.get("delta") or "")
        elif event_type == "assistant.thinking.delta":
            spec = by_id.get(str(payload.get("messageId") or ""))
            if spec is not None:
                spec["thinking"] = str(spec.get("thinking") or "") + str(payload.get("delta") or "")
        elif event_type == "tool.started":
            spec = by_id.get(str(payload.get("messageId") or ""))
            tool_use_id = str(payload.get("toolUseId") or "")
            if spec is None or not tool_use_id:
                continue
            tool = tools.setdefault(tool_use_id, {"toolUseId": tool_use_id})
            tool["toolName"] = payload.get("toolName") or tool.get("toolName") or ""
            tool.setdefault("input", "")
            tool.setdefault("results", [])
            tool["status"] = "running"
            tool["stored"] = True
            if tool_use_id not in spec["toolUseIds"]:
                spec["toolUseIds"].append(tool_use_id)
            spec["tools"][tool_use_id] = tool
        elif event_type == "tool.input.delta":
            tool_use_id = str(payload.get("toolUseId") or "")
            tool = tools.get(tool_use_id)
            if tool is None:
                continue
            tool["input"] = str(tool.get("input") or "") + str(payload.get("delta") or "")
        elif event_type == "tool.result":
            tool_use_id = str(payload.get("toolUseId") or "")
            tool = tools.get(tool_use_id)
            if tool is None:
                continue
            tool["resultKind"] = payload.get("resultKind")
            tool["summary"] = payload.get("summary")
            tool.setdefault("results", []).append(
                {
                    "content": payload.get("content"),
                    "summary": payload.get("summary"),
                    "isError": payload.get("isError"),
                }
            )
            tool["artifacts"] = payload.get("artifacts") or []
        elif event_type == "tool.finished":
            tool_use_id = str(payload.get("toolUseId") or "")
            tool = tools.get(tool_use_id)
            if tool is None:
                continue
            tool["status"] = payload.get("status") or tool.get("status") or "completed"
            if payload.get("summary"):
                tool["summary"] = payload.get("summary")
        elif event_type == "pipeline.event" and payload.get("kind") in {
            "stack.progress",
            "stack.instances.progress",
        }:
            # Attach the latest stack progress frame to the tool card so a reload
            # renders the same REPL-style resource table the live stream showed
            # (mirrors events.js ``case "pipeline.event"``). Overwrite-style: keep
            # only the newest frame, matching the live reducer.
            tool_use_id = str(payload.get("toolUseId") or "")
            tool = tools.get(tool_use_id)
            if tool is None:
                continue
            tool["stackProgress"] = {
                "kind": payload.get("kind"),
                "stackName": payload.get("stackName"),
                "stackGroupName": payload.get("stackGroupName"),
                "stackId": payload.get("stackId"),
                "operationId": payload.get("operationId"),
                "regionId": payload.get("regionId"),
                "status": payload.get("status"),
                "progressPercentage": payload.get("progressPercentage"),
                "resources": payload.get("resources"),
                "instances": payload.get("instances"),
                "elapsedSeconds": payload.get("elapsedSeconds"),
                "deploymentComplete": payload.get("deploymentComplete") is True,
            }

    # Tag each ``input_required`` prompt bubble with how many answers it anchors
    # (normally 1). The reload path (``load_visible_transcript``) reads this to
    # weave persisted ``source=pipeline`` replies right after their prompt rather
    # than appending them after the whole replay (Issue 2 misordering).
    for anchor_id in translator.input_prompt_message_ids:
        spec = by_id.get(anchor_id)
        if spec is not None:
            spec["inputAnswerSlots"] = int(spec.get("inputAnswerSlots") or 0) + 1

    return specs
