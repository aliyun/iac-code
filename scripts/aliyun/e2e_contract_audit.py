from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.aliyun.result_contract import aliyun_http_from_metadata


def find_latest_aliyun_tool_result(config_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Return the newest persisted ToolResult carrying internal Aliyun metadata."""

    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for path in (Path(config_dir) / "projects").rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = row.get("content") if isinstance(row, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("metadata"), dict)
                    and "aliyun_http" in block["metadata"]
                ):
                    candidates.append((path.stat().st_mtime_ns, path, block))
    if not candidates:
        raise AssertionError("no persisted aliyun_api ToolResult was found")
    _, path, block = max(candidates, key=lambda item: item[0])
    return path, block


def audit_aliyun_result_contract(
    *,
    expected_body: Any,
    tool_result_content: str,
    tool_result_metadata: Mapping[str, Any],
    resumed_content: str | None = None,
    resumed_metadata: Mapping[str, Any] | None = None,
    public_payloads: Iterable[Any] = (),
    forbidden_values: Iterable[str] = (),
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Check the reviewed business-body and internal-metadata boundaries."""

    failures: list[dict[str, Any]] = []
    actual_body = _parse_json(tool_result_content)
    if actual_body != expected_body:
        failures.append({"check": "business_body", "expected": expected_body, "actual": actual_body})

    http_metadata = aliyun_http_from_metadata(tool_result_metadata)
    if http_metadata is None:
        failures.append({"check": "internal_aliyun_http_present", "actual": tool_result_metadata})

    if resumed_content is not None:
        resumed_body = _parse_json(resumed_content)
        if resumed_body != expected_body:
            failures.append({"check": "resumed_business_body", "expected": expected_body, "actual": resumed_body})
        resumed_http = aliyun_http_from_metadata(resumed_metadata or {})
        if resumed_http != http_metadata:
            failures.append({"check": "resumed_aliyun_http", "expected": http_metadata, "actual": resumed_http})

    leak_paths: list[dict[str, Any]] = []
    forbidden = tuple(value for value in forbidden_values if value)
    for index, payload in enumerate(public_payloads):
        _collect_leaks(payload, path=f"public_payloads[{index}]", forbidden_values=forbidden, output=leak_paths)
    if leak_paths:
        failures.append({"check": "public_payload_has_no_internal_metadata", "leaks": leak_paths})

    result = {
        "passed": not failures,
        "business_body": actual_body,
        "aliyun_http": http_metadata,
        "failures": failures,
    }
    if output_path is not None:
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def audit_public_payloads(
    public_payloads: Iterable[Any],
    *,
    forbidden_values: Iterable[str] = (),
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Audit external payloads without requiring an Aliyun ToolResult."""

    leak_paths: list[dict[str, Any]] = []
    forbidden = tuple(value for value in forbidden_values if value)
    for index, payload in enumerate(public_payloads):
        _collect_leaks(payload, path=f"public_payloads[{index}]", forbidden_values=forbidden, output=leak_paths)
    result = {"passed": not leak_paths, "leaks": leak_paths}
    if output_path is not None:
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _parse_json(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def _collect_leaks(value: Any, *, path: str, forbidden_values: tuple[str, ...], output: list[dict[str, Any]]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key == "aliyun_http":
                output.append({"path": child_path, "reason": "internal_key"})
            _collect_leaks(item, path=child_path, forbidden_values=forbidden_values, output=output)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _collect_leaks(item, path=f"{path}[{index}]", forbidden_values=forbidden_values, output=output)
        return
    if isinstance(value, str):
        for forbidden in forbidden_values:
            if forbidden in value:
                output.append({"path": path, "reason": "forbidden_value"})
                break
