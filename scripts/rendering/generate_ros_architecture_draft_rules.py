#!/usr/bin/env python3
"""Generate LLM draft architecture rules from ROS resource facts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from rich.console import Console

from iac_code.config import DEFAULT_MODEL, get_config_dir, load_credentials, load_saved_model
from iac_code.pipeline.engine.architecture_rule_drafts import (
    apply_reviewed_draft_patches,
    parse_draft_rules_response,
    render_draft_review_report_markdown,
    review_valid_draft_rules,
    validate_draft_rules,
)
from iac_code.pipeline.engine.architecture_rule_facts import ResourceRuleFact, ResourceRuleFactsBundle, RuleSignal
from iac_code.providers.base import Message
from iac_code.providers.manager import ProviderManager

SYSTEM_PROMPT = """\
You analyze Alibaba Cloud ROS resource type facts and propose architecture diagram rules.
Return ONLY valid JSON, no markdown.

Schema:
{"draft_rules":[{"resource_type":string,"classification":"core_node|container|attachment|bridge_attachment|attachment_edge|orchestration_action|concept_node|supplemental_relation|display|ignore|needs_review","target_resource_types":[string],"property_names":[string],"label":string|object|null,"edge_label":string|object|null,"confidence":number,"evidence":[string],"suggested_architecture_rules_patch":object}]}

Rules:
- Use only resource_type and property names present in the input facts.
- Treat rule_signals as hints, not final decisions.
- Configuration, binding, rule, entry, whitelist, command, and invocation resources should usually be attachments,
  bridge attachments, attachment edges, or orchestration actions, not core visible nodes.
- Output ONLY actionable draft rules that should change architecture_rules.json. Omit resources that should stay
  core_node, ignore, or needs_review. An empty {"draft_rules": []} is valid for a chunk with no fixed-rule changes.
- Do not use solid business traffic edges for attachment/configuration relationships.
- For every actionable draft, suggested_architecture_rules_patch is REQUIRED and must use one of these shapes:
  attachment ->
    {"compact_child_attachments":[{"resource_types":[...],"target_properties":[...],"target_types":[...],"label":...}]}
  bridge_attachment ->
    {"compact_bridge_attachments":[{"resource_types":[...],"source_properties":[...],
    "via_resource_types":[...],"via_source_properties":[...],"via_target_properties":[...],
    "target_types":[...],"label":...}]}
  attachment_edge ->
    {"compact_attachment_edges":[{"resource_types":[...],"source_properties":[...],
    "marker_properties":[...],"source_types":[...],"marker_types":[...],
    "edge_style":"dotted_open","edge_label":...}]}
  orchestration_action ->
    {"compact_orchestration_actions":[{"resource_types":[...],"command_properties":[...],
    "target_properties":[...],"evidence_properties":[...]}]}
  concept_node ->
    {"compact_concept_nodes":[{"via_resource_types":[...],"controller_property":"...",
    "source_property":"...","id_suffix":"...","resource_type":"CONCEPT::...","label":...}]}
  container -> {"network_layer_types":[...],"containment_layer_types":{"role":[...]}}
- Prefer conservative `needs_review` when evidence is weak.
"""

DEFAULT_ACTIONABLE_SIGNAL_CATEGORIES = (
    "container",
    "attachment",
    "bridge_attachment",
    "attachment_edge",
    "orchestration_action",
    "concept_node",
)
PROMPT_VERSION = "ros-architecture-draft-rules-v2-bilingual-labels"


def default_output_dir() -> Path:
    return get_config_dir() / "architecture"


def default_facts_path() -> Path:
    return default_output_dir() / "ros-resource-facts.json"


def default_cache_path() -> Path:
    return default_output_dir() / "ros-draft-rules-cache.json"


def default_draft_out() -> Path:
    return default_output_dir() / "ros-draft-rules.json"


def default_review_out() -> Path:
    return default_output_dir() / "ros-draft-rules-review.md"


def default_final_report_out() -> Path:
    return default_output_dir() / "ros-draft-rules-final-report.md"


def default_rules_diff_out() -> Path:
    return default_output_dir() / "ros-draft-rules-diff-summary.json"


def default_backup_path() -> Path:
    return default_output_dir() / "architecture_rules.json.backup"


def chunk_resource_facts(
    facts_payload: dict[str, Any],
    *,
    max_facts_per_chunk: int = 40,
    mode: str = "product",
    actionable_only: bool = False,
    signal_categories: tuple[str, ...] | None = None,
    signal_confidences: tuple[str, ...] | None = None,
    max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    facts = [fact for fact in facts_payload.get("resource_facts", []) if isinstance(fact, dict)]
    signals = [signal for signal in facts_payload.get("rule_signals", []) if isinstance(signal, dict)]
    categories = tuple(signal_categories or ())
    confidences = tuple(signal_confidences or ())
    selected_resource_types = {
        signal["resource_type"]
        for signal in signals
        if isinstance(signal.get("resource_type"), str)
        and _include_signal_in_llm_prompt(signal)
        and _signal_category_allowed(signal, categories)
        and _signal_confidence_allowed(signal, confidences)
    }
    if actionable_only:
        facts = [fact for fact in facts if fact.get("resource_type") in selected_resource_types]
        signals = [
            signal
            for signal in signals
            if signal.get("resource_type") in selected_resource_types
            and _include_signal_in_llm_prompt(signal)
            and _signal_category_allowed(signal, categories)
            and _signal_confidence_allowed(signal, confidences)
        ]
    elif categories:
        signals = [signal for signal in signals if _signal_category_allowed(signal, categories)]
    if confidences and not actionable_only:
        signals = [signal for signal in signals if _signal_confidence_allowed(signal, confidences)]
    signals_by_resource: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        resource_type = signal.get("resource_type")
        if isinstance(resource_type, str):
            signals_by_resource.setdefault(resource_type, []).append(signal)

    facts_by_product: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        product_code = fact.get("product_code")
        if isinstance(product_code, str) and product_code:
            facts_by_product.setdefault(product_code, []).append(fact)

    if mode == "global":
        sorted_facts = sorted(
            facts,
            key=lambda item: (str(item.get("product_code") or ""), str(item.get("resource_type") or "")),
        )
        chunks: list[dict[str, Any]] = []
        chunk_size = max(1, max_facts_per_chunk)
        for index in range(0, len(sorted_facts), chunk_size):
            current_facts = sorted_facts[index : index + chunk_size]
            current_resource_types = {
                fact["resource_type"] for fact in current_facts if isinstance(fact.get("resource_type"), str)
            }
            chunks.append(
                {
                    "chunk_id": f"all-{index // chunk_size + 1}",
                    "product_code": "all",
                    "resource_facts": current_facts,
                    "rule_signals": [
                        signal
                        for resource_type in sorted(current_resource_types)
                        for signal in signals_by_resource.get(resource_type, [])
                    ],
                }
            )
        return chunks[:max_chunks] if max_chunks is not None else chunks

    chunks: list[dict[str, Any]] = []
    for product_code in sorted(facts_by_product):
        product_facts = sorted(facts_by_product[product_code], key=lambda item: str(item.get("resource_type") or ""))
        for index in range(0, len(product_facts), max(1, max_facts_per_chunk)):
            current_facts = product_facts[index : index + max(1, max_facts_per_chunk)]
            suffix = "" if len(product_facts) <= max_facts_per_chunk else f"-{index // max_facts_per_chunk + 1}"
            current_resource_types = {
                fact["resource_type"] for fact in current_facts if isinstance(fact.get("resource_type"), str)
            }
            chunks.append(
                {
                    "chunk_id": f"{product_code}{suffix}",
                    "product_code": product_code,
                    "resource_facts": current_facts,
                    "rule_signals": [
                        signal
                        for resource_type in sorted(current_resource_types)
                        for signal in signals_by_resource.get(resource_type, [])
                    ],
                }
            )
    return chunks[:max_chunks] if max_chunks is not None else chunks


def build_draft_rules_prompt(chunk: dict[str, Any]) -> str:
    compact_chunk = _compact_chunk_for_llm(chunk)
    return (
        "Create draft architecture rules for this Resource Facts / Rule Signals chunk.\n"
        "Return JSON only.\n\n"
        "For label and edge_label, prefer bilingual objects like "
        "{\"zh\":\"中文短标签\",\"en\":\"English short label\"}; "
        "use null only when the classification does not render a visible label.\n\n"
        f"{json.dumps(compact_chunk, ensure_ascii=False)}"
    )


def chunk_fingerprint(chunk: dict[str, Any]) -> str:
    compact_chunk = _compact_chunk_for_llm(chunk)
    payload = json.dumps(
        {"prompt_version": PROMPT_VERSION, "chunk": compact_chunk},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def complete_chunk_with_llm(prompt: str, *, model: str) -> str:
    manager = ProviderManager(model=model, credentials=load_credentials(model=model))
    response = await manager.complete([Message.user(prompt)], SYSTEM_PROMPT, max_tokens=4000)
    return response.text


def run_draft_generation_from_facts_payload(
    facts_payload: dict[str, Any],
    *,
    cache_path: Path,
    max_facts_per_chunk: int,
    complete_chunk: Callable[[str], str | Awaitable[str]],
    chunk_mode: str = "product",
    actionable_only: bool = False,
    signal_categories: tuple[str, ...] | None = None,
    signal_confidences: tuple[str, ...] | None = None,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_draft_generation_from_facts_payload_async(
            facts_payload,
            cache_path=cache_path,
            max_facts_per_chunk=max_facts_per_chunk,
            complete_chunk=complete_chunk,
            chunk_mode=chunk_mode,
            actionable_only=actionable_only,
            signal_categories=signal_categories,
            signal_confidences=signal_confidences,
            max_chunks=max_chunks,
        )
    )


async def run_draft_generation_from_facts_payload_async(
    facts_payload: dict[str, Any],
    *,
    cache_path: Path,
    max_facts_per_chunk: int,
    complete_chunk: Callable[[str], str | Awaitable[str]],
    chunk_mode: str = "product",
    actionable_only: bool = False,
    signal_categories: tuple[str, ...] | None = None,
    signal_confidences: tuple[str, ...] | None = None,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    cache = _load_cache(cache_path)
    chunks_cache = cache.setdefault("chunks", {})
    draft_rules: list[dict[str, Any]] = []
    chunk_summaries: list[dict[str, Any]] = []
    parse_errors = 0
    categories = tuple(signal_categories or ())
    confidences = tuple(signal_confidences or ())

    chunks = chunk_resource_facts(
        facts_payload,
        max_facts_per_chunk=max_facts_per_chunk,
        mode=chunk_mode,
        actionable_only=actionable_only,
        signal_categories=categories,
        signal_confidences=confidences,
        max_chunks=max_chunks,
    )
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        fingerprint = chunk_fingerprint(chunk)
        cached = chunks_cache.get(chunk_id)
        if (
            isinstance(cached, dict)
            and cached.get("fingerprint") == fingerprint
            and isinstance(cached.get("response_text"), str)
        ):
            response_text = cached["response_text"]
            from_cache = True
        else:
            response_text = await _run_complete_chunk(complete_chunk, build_draft_rules_prompt(chunk))
            chunks_cache[chunk_id] = {"fingerprint": fingerprint, "response_text": response_text}
            _write_cache(cache_path, cache)
            from_cache = False
        parse_error = None
        try:
            parsed = [draft.to_dict() for draft in parse_draft_rules_response(response_text)]
        except ValueError as exc:
            parsed = []
            parse_error = str(exc)
            parse_errors += 1
        draft_rules.extend(parsed)
        summary = {
            "chunk_id": chunk_id,
            "from_cache": from_cache,
            "resource_facts": len(chunk["resource_facts"]),
            "draft_rules": len(parsed),
        }
        if parse_error is not None:
            summary["parse_error"] = parse_error
        chunk_summaries.append(summary)

    _write_cache(cache_path, cache)
    draft_rules.sort(key=lambda item: (item.get("resource_type") or "", item.get("classification") or ""))
    return {
        "summary": {
            "chunks": chunk_summaries,
            "draft_rules": len(draft_rules),
            "parse_errors": parse_errors,
            "input_resource_facts": len(
                [fact for fact in facts_payload.get("resource_facts", []) if isinstance(fact, dict)]
            ),
            "llm_resource_facts": sum(len(chunk.get("resource_facts", [])) for chunk in chunks),
            "actionable_only": actionable_only,
            "signal_categories": list(categories),
            "signal_confidences": list(confidences),
        },
        "draft_rules": draft_rules,
    }


def build_facts_bundle_from_payload(payload: dict[str, Any]) -> ResourceRuleFactsBundle:
    facts = tuple(
        ResourceRuleFact(
            resource_type=fact["resource_type"],
            product_code=fact.get("product_code") or "",
            source_state=fact.get("source_state") or "",
            name=fact.get("name") if isinstance(fact.get("name"), dict) else {"zh": None, "en": None},
            description=fact.get("description") if isinstance(fact.get("description"), str) else None,
            category_code=fact.get("category_code") if isinstance(fact.get("category_code"), str) else None,
            properties=fact.get("properties") if isinstance(fact.get("properties"), dict) else {},
            related_properties=(
                fact.get("related_properties") if isinstance(fact.get("related_properties"), dict) else {}
            ),
            main_resource_type=(
                fact.get("main_resource_type") if isinstance(fact.get("main_resource_type"), dict) else None
            ),
            fixed_rule_hits=tuple(
                item for item in fact.get("fixed_rule_hits", []) if isinstance(item, str)
            ),
        )
        for fact in payload.get("resource_facts", [])
        if isinstance(fact, dict) and isinstance(fact.get("resource_type"), str)
    )
    signals = tuple(
        RuleSignal(
            category=signal.get("category") or "",
            resource_type=signal.get("resource_type") or "",
            product_code=signal.get("product_code") or "",
            property_name=signal.get("property_name") if isinstance(signal.get("property_name"), str) else None,
            target_resource_types=tuple(
                item for item in signal.get("target_resource_types", []) if isinstance(item, str)
            ),
            confidence=signal.get("confidence") or "",
            evidence=tuple(item for item in signal.get("evidence", []) if isinstance(item, str)),
            suggested_patch=signal.get("suggested_patch") if isinstance(signal.get("suggested_patch"), dict) else {},
        )
        for signal in payload.get("rule_signals", [])
        if isinstance(signal, dict) and isinstance(signal.get("resource_type"), str)
    )
    summary_raw = payload.get("summary")
    summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
    return ResourceRuleFactsBundle(
        resource_facts=facts,
        rule_signals=signals,
        api_resource_type_count=int(summary.get("api_resource_types") or len(facts)),
        local_resource_type_count=int(summary.get("local_resource_types") or 0),
        api_only_resource_types=(),
        local_only_resource_types=(),
        fetch_errors={},
    )


def load_existing_draft_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("draft_rules"), list):
        raise ValueError("draft result must be a JSON object with draft_rules list")
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    return {"summary": summary, "draft_rules": value["draft_rules"]}


def filter_draft_result(result: dict[str, Any], *, reject_resource_types: tuple[str, ...]) -> dict[str, Any]:
    rejected = set(reject_resource_types)
    if not rejected:
        return result
    draft_rules = [
        draft
        for draft in result.get("draft_rules", [])
        if not (isinstance(draft, dict) and draft.get("resource_type") in rejected)
    ]
    summary_raw = result.get("summary")
    summary: dict[str, Any] = dict(summary_raw) if isinstance(summary_raw, dict) else {}
    summary["draft_rules"] = len(draft_rules)
    summary["subagent_rejected_resource_types"] = sorted(rejected)
    return {"summary": summary, "draft_rules": draft_rules}


def summarize_rules_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, int | bool]]:
    summary: dict[str, dict[str, int | bool]] = {}
    for key in sorted(set(before) | set(after)):
        before_value = before.get(key)
        after_value = after.get(key)
        if isinstance(before_value, list) or isinstance(after_value, list):
            before_len = len(before_value) if isinstance(before_value, list) else 0
            after_len = len(after_value) if isinstance(after_value, list) else 0
            if before_len != after_len:
                summary[key] = {
                    "before": before_len,
                    "after": after_len,
                    "added": max(0, after_len - before_len),
                    "removed": max(0, before_len - after_len),
                }
        elif isinstance(before_value, dict) or isinstance(after_value, dict):
            before_dict = before_value if isinstance(before_value, dict) else {}
            after_dict = after_value if isinstance(after_value, dict) else {}
            added = set(after_dict) - set(before_dict)
            removed = set(before_dict) - set(after_dict)
            changed = {
                item
                for item in set(before_dict) & set(after_dict)
                if json.dumps(before_dict[item], ensure_ascii=False, sort_keys=True)
                != json.dumps(after_dict[item], ensure_ascii=False, sort_keys=True)
            }
            if added or removed or changed:
                summary[key] = {
                    "before": len(before_dict),
                    "after": len(after_dict),
                    "dict_added": len(added),
                    "dict_removed": len(removed),
                    "dict_changed": len(changed),
                }
        elif before_value != after_value:
            summary[key] = {"changed": True}
    return summary


def render_final_report_markdown(
    *,
    facts_payload: dict[str, Any],
    draft_result: dict[str, Any],
    review_markdown: str,
    diff_summary: dict[str, dict[str, int | bool]],
    paths: dict[str, str],
    verification_results: list[str] | None = None,
    blockers: list[str] | None = None,
) -> str:
    facts_summary_raw = facts_payload.get("summary")
    facts_summary: dict[str, Any] = facts_summary_raw if isinstance(facts_summary_raw, dict) else {}
    draft_summary_raw = draft_result.get("summary")
    draft_summary: dict[str, Any] = draft_summary_raw if isinstance(draft_summary_raw, dict) else {}
    accepted, rejected = _extract_review_counts(review_markdown)
    lines = [
        "# ROS 架构图规则提取最终报告",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| ROS API 资源类型 | {facts_summary.get('api_resource_types', '-')} |",
        f"| Resource Facts | {facts_summary.get('resource_facts', '-')} |",
        f"| Rule Signals | {facts_summary.get('rule_signals', '-')} |",
        f"| LLM 输入资源类型 | {draft_summary.get('llm_resource_facts', '-')} |",
        f"| LLM 草案数量 | {draft_summary.get('draft_rules', 0)} |",
        f"| LLM 解析错误 | {draft_summary.get('parse_errors', 0)} |",
        f"| Review 通过 | {accepted if accepted is not None else '-'} |",
        f"| Review 拒绝 | {rejected if rejected is not None else '-'} |",
        "",
        "## Artifacts",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    for name in sorted(paths):
        lines.append(f"| {name} | `{paths[name]}` |")
    lines.extend(["", "## architecture_rules.json Diff Summary", ""])
    if diff_summary:
        lines.extend(["| Rule key | Summary |", "| --- | --- |"])
        for key, value in sorted(diff_summary.items()):
            parts = [f"{field}={count}" for field, count in value.items()]
            lines.append(f"| `{key}` | {', '.join(parts)} |")
    else:
        lines.append("No accepted rule changed `architecture_rules.json`.")
    lines.extend(["", "## Verification", ""])
    for item in verification_results or []:
        lines.append(f"- {item}")
    if not verification_results:
        lines.append("- Not run yet.")
    lines.extend(["", "## Blockers", ""])
    if blockers:
        for blocker in blockers:
            lines.append(f"- {blocker}")
    else:
        lines.append("- None recorded.")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate LLM draft ROS architecture rules from resource facts.")
    parser.add_argument("--facts-json", type=Path, default=None, help="Resource facts JSON path.")
    parser.add_argument("--cache", type=Path, default=None, help="Draft generation cache path.")
    parser.add_argument("--draft-in", type=Path, default=None, help="Existing draft rules JSON to review/apply.")
    parser.add_argument("--draft-out", type=Path, default=None, help="Draft rules JSON output path.")
    parser.add_argument("--review-out", type=Path, default=None, help="Draft review Markdown output path.")
    parser.add_argument("--final-report-out", type=Path, default=None, help="Final Markdown report output path.")
    parser.add_argument("--rules-diff-out", type=Path, default=None, help="Rule diff summary JSON output path.")
    parser.add_argument("--rules-in", type=Path, default=None, help="architecture_rules.json input path.")
    parser.add_argument("--rules-out", type=Path, default=None, help="architecture_rules.json output path.")
    parser.add_argument("--backup-path", type=Path, default=None, help="Required backup path when --apply is used.")
    parser.add_argument("--apply", action="store_true", help="Apply accepted reviewed patches to rules-out.")
    parser.add_argument("--model", default=None, help="Override model. Defaults to saved iac-code model.")
    parser.add_argument("--max-facts-per-chunk", type=int, default=40, help="Maximum facts per LLM chunk.")
    parser.add_argument("--max-chunks", type=int, default=None, help="Maximum LLM chunks to process this run.")
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help="Only send resources with selected non-display rule signals to the LLM.",
    )
    parser.add_argument(
        "--signal-categories",
        default=",".join(DEFAULT_ACTIONABLE_SIGNAL_CATEGORIES),
        help="Comma-separated rule signal categories to send to the LLM when filtering.",
    )
    parser.add_argument(
        "--signal-confidences",
        default="",
        help="Optional comma-separated signal confidence values to send to the LLM, such as high,medium.",
    )
    parser.add_argument(
        "--reject-resource-types",
        default="",
        help="Comma-separated resource types rejected by subagent review before applying patches.",
    )
    parser.add_argument(
        "--chunk-mode",
        choices=("product", "global"),
        default="product",
        help="Chunking strategy. product keeps product boundaries; global packs products into fewer LLM calls.",
    )
    parser.add_argument("--print-raw", action="store_true", help="Print generated draft JSON.")
    return parser.parse_args(argv)


async def async_cli_main(argv: list[str]) -> int:
    args = parse_args(argv)
    console = Console()
    facts_path = args.facts_json or default_facts_path()
    cache_path = args.cache or default_cache_path()
    draft_out = args.draft_out or default_draft_out()
    review_out = args.review_out or default_review_out()
    final_report_out = args.final_report_out or default_final_report_out()
    rules_diff_out = args.rules_diff_out or default_rules_diff_out()
    rules_in = args.rules_in or Path("src/iac_code/pipeline/engine/architecture_rules.json")
    rules_out = args.rules_out or rules_in
    backup_path = args.backup_path or default_backup_path()
    model = args.model or load_saved_model() or DEFAULT_MODEL
    signal_categories = _parse_signal_categories(args.signal_categories)
    signal_confidences = _parse_csv_tuple(args.signal_confidences)
    reject_resource_types = _parse_csv_tuple(args.reject_resource_types)

    facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))

    async def real_complete(prompt: str) -> str:
        return await complete_chunk_with_llm(prompt, model=model)

    if args.draft_in is not None:
        result = load_existing_draft_result(args.draft_in)
    else:
        result = await run_draft_generation_from_facts_payload_async(
            facts_payload,
            cache_path=cache_path,
            max_facts_per_chunk=args.max_facts_per_chunk,
            chunk_mode=args.chunk_mode,
            actionable_only=args.actionable_only,
            signal_categories=signal_categories,
            signal_confidences=signal_confidences,
            max_chunks=args.max_chunks,
            complete_chunk=real_complete,
        )
        draft_out.parent.mkdir(parents=True, exist_ok=True)
        draft_out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    result = filter_draft_result(result, reject_resource_types=reject_resource_types)

    facts_bundle = build_facts_bundle_from_payload(facts_payload)
    drafts = parse_draft_rules_response(json.dumps({"draft_rules": result["draft_rules"]}, ensure_ascii=False))
    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts_bundle))
    review_out.parent.mkdir(parents=True, exist_ok=True)
    review_markdown = render_draft_review_report_markdown(reviewed)
    review_out.write_text(review_markdown, encoding="utf-8")
    diff_summary: dict[str, dict[str, int | bool]] = {}

    if args.apply:
        if not backup_path.is_file():
            console.print(f"[red]Backup is required before applying rules: {backup_path}[/]")
            return 2
        raw_rules = json.loads(rules_in.read_text(encoding="utf-8"))
        updated = apply_reviewed_draft_patches(raw_rules, reviewed, facts=facts_bundle)
        diff_summary = summarize_rules_diff(raw_rules, updated)
        rules_out.parent.mkdir(parents=True, exist_ok=True)
        rules_out.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rules_diff_out.parent.mkdir(parents=True, exist_ok=True)
        rules_diff_out.write_text(
            json.dumps(diff_summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    final_report = render_final_report_markdown(
        facts_payload=facts_payload,
        draft_result=result,
        review_markdown=review_markdown,
        diff_summary=diff_summary,
        paths={
            "facts": str(facts_path),
            "draft": str(draft_out if args.draft_in is None else args.draft_in),
            "review": str(review_out),
            "rules": str(rules_out),
            "rules_diff": str(rules_diff_out),
        },
    )
    final_report_out.parent.mkdir(parents=True, exist_ok=True)
    final_report_out.write_text(final_report, encoding="utf-8")

    if args.print_raw:
        console.print_json(json.dumps(result, ensure_ascii=False))
    if args.draft_in is None:
        console.print(f"[green]Wrote draft rules:[/] {draft_out}")
    else:
        console.print(f"[green]Loaded draft rules:[/] {args.draft_in}")
    console.print(f"[green]Wrote review report:[/] {review_out}")
    console.print(f"[green]Wrote final report:[/] {final_report_out}")
    console.print(f"[dim]Cache:[/] {cache_path}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_cli_main(sys.argv[1:])))


def _load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {"chunks": {}}
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"chunks": {}}
    return value if isinstance(value, dict) else {"chunks": {}}


def _write_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


async def _run_complete_chunk(complete_chunk: Callable[[str], str | Awaitable[str]], prompt: str) -> str:
    value = complete_chunk(prompt)
    if inspect.isawaitable(value):
        return await cast(Awaitable[str], value)
    return value


def _compact_chunk_for_llm(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk.get("chunk_id"),
        "product_code": chunk.get("product_code"),
        "resource_facts": [_compact_fact(fact) for fact in chunk.get("resource_facts", []) if isinstance(fact, dict)],
        "rule_signals": [
            _compact_signal(signal)
            for signal in chunk.get("rule_signals", [])
            if isinstance(signal, dict) and _include_signal_in_llm_prompt(signal)
        ],
    }


def _compact_fact(fact: dict[str, Any]) -> dict[str, Any]:
    properties_raw = fact.get("properties")
    properties: dict[str, Any] = properties_raw if isinstance(properties_raw, dict) else {}
    related_properties_raw = fact.get("related_properties")
    related_properties: dict[str, Any] = (
        related_properties_raw if isinstance(related_properties_raw, dict) else {}
    )
    main_resource_type_raw = fact.get("main_resource_type")
    main_resource_type: dict[str, Any] | None = (
        main_resource_type_raw if isinstance(main_resource_type_raw, dict) else None
    )
    return {
        "resource_type": fact.get("resource_type"),
        "product_code": fact.get("product_code"),
        "source_state": fact.get("source_state"),
        "name": fact.get("name"),
        "description": _shorten(fact.get("description"), limit=120),
        "category_code": fact.get("category_code"),
        "property_names": sorted(name for name in properties if isinstance(name, str)),
        "properties": _compact_properties(properties, related_properties, main_resource_type),
        "related_properties": related_properties,
        "main_resource_type": main_resource_type,
        "fixed_rule_hits": fact.get("fixed_rule_hits") if isinstance(fact.get("fixed_rule_hits"), list) else [],
    }


def _compact_properties(
    raw: Any,
    related_properties: dict[str, Any],
    main_resource_type: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    interesting_names = set(related_properties)
    if main_resource_type is not None and isinstance(main_resource_type.get("ref_property"), str):
        interesting_names.add(main_resource_type["ref_property"])
    compact: dict[str, dict[str, Any]] = {}
    selected = 0
    for name, value in sorted(raw.items()):
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        property_info = cast(dict[str, Any], value)
        if name not in interesting_names and not _looks_interesting_property_name(name):
            continue
        if selected >= 24 and name not in interesting_names:
            continue
        compact[name] = {
            "type": property_info.get("type"),
            "required": property_info.get("required"),
            "description": _shorten(property_info.get("description"), limit=100),
            "related_targets": property_info.get("related_targets")
            if isinstance(property_info.get("related_targets"), list)
            else [],
        }
        selected += 1
    return compact


def _compact_signal(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": signal.get("category"),
        "resource_type": signal.get("resource_type"),
        "property_name": signal.get("property_name"),
        "target_resource_types": signal.get("target_resource_types")
        if isinstance(signal.get("target_resource_types"), list)
        else [],
        "confidence": signal.get("confidence"),
        "evidence": [_shorten(item) for item in signal.get("evidence", []) if isinstance(item, str)],
    }


def _include_signal_in_llm_prompt(signal: dict[str, Any]) -> bool:
    category = signal.get("category")
    if category == "display":
        return False
    if category == "supplemental_relation":
        targets = signal.get("target_resource_types")
        return isinstance(targets, list) and bool(targets)
    return True


def _signal_category_allowed(signal: dict[str, Any], categories: tuple[str, ...]) -> bool:
    if not categories:
        return True
    return signal.get("category") in categories


def _signal_confidence_allowed(signal: dict[str, Any], confidences: tuple[str, ...]) -> bool:
    if not confidences:
        return True
    return signal.get("confidence") in confidences


def _parse_signal_categories(raw: str) -> tuple[str, ...]:
    categories = _parse_csv_tuple(raw)
    return categories or DEFAULT_ACTIONABLE_SIGNAL_CATEGORIES


def _parse_csv_tuple(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _extract_review_counts(markdown: str) -> tuple[int | None, int | None]:
    accepted = None
    rejected = None
    for line in markdown.splitlines():
        normalized = line.strip()
        if normalized.startswith("| Accepted |"):
            accepted = _last_table_int(normalized)
        elif normalized.startswith("| Rejected |"):
            rejected = _last_table_int(normalized)
    return accepted, rejected


def _last_table_int(line: str) -> int | None:
    parts = [part.strip() for part in line.strip("|").split("|")]
    for part in reversed(parts):
        try:
            return int(part)
        except ValueError:
            continue
    return None


def _shorten(value: Any, *, limit: int = 220) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _looks_interesting_property_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(("id", "ids", "name", "names")) or any(
        token in lowered
        for token in (
            "access",
            "bandwidth",
            "command",
            "eip",
            "entry",
            "gateway",
            "group",
            "instance",
            "mount",
            "package",
            "policy",
            "rule",
            "table",
            "target",
            "vpc",
            "vswitch",
        )
    )


if __name__ == "__main__":
    main()
