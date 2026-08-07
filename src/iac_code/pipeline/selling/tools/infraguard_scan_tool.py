"""InfraGuard scan tool for pipeline review gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from iac_code.desktop.external_env import create_subprocess_exec, guarded_command, spawn_env
from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult

_SCAN_TIMEOUT_SECONDS = 120
_TOOL_TIMEOUT_BUFFER_SECONDS = 15


async def _desktop_consumer_lease() -> tuple[Any | None, str | None]:
    """Acquire the Desktop shared lease without changing non-Desktop scans."""
    if os.environ.get("IAC_CODE_DESKTOP_RUNTIME") != "1":
        return None, None
    lock_dir = os.environ.get("IAC_CODE_DESKTOP_INSTALL_LOCK_DIR")
    managed_path = os.environ.get("IAC_CODE_DESKTOP_INFRAGUARD_PATH")
    if not lock_dir or not managed_path:
        return None, "recovery_required"
    from iac_code.desktop.download_journal import DesktopPrerequisiteConsumerLease

    lease = DesktopPrerequisiteConsumerLease(
        Path(lock_dir),
        Path(managed_path),
        prerequisite="infraguard",
        timeout=5.0,
    )
    acquire = asyncio.create_task(asyncio.to_thread(lease.__enter__))
    try:
        await asyncio.shield(acquire)
    except asyncio.CancelledError:
        # A worker thread cannot be cancelled while it polls an OS lock.  Wait for
        # its bounded result and release immediately so cancellation never leaks a
        # reader that would block repair in another Desktop channel.
        with suppress(Exception):
            await asyncio.shield(acquire)
            await asyncio.to_thread(lease.__exit__, None, None, None)
        raise
    except TimeoutError:
        return None, "installing"
    if lease.recovery_required():
        await asyncio.to_thread(lease.__exit__, None, None, None)
        return None, "recovery_required"
    return lease, None


def _desktop_prerequisite_error(status: str, file_path: str) -> ToolResult:
    return ToolResult(
        content=json.dumps(
            {
                "error": status,
                "prerequisite": "infraguard",
                "file_path": file_path,
            },
            ensure_ascii=False,
        ),
        is_error=True,
    )


async def _run_infraguard_with_desktop_lease(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: float,
    env: dict[str, str] | None,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    lease, blocked = await _desktop_consumer_lease()
    if blocked is not None:
        return None, blocked
    try:
        managed_command = command
        if lease is not None and command and command[0] == "infraguard":
            # On Windows, CreateProcess resolves a bare executable before the
            # child process receives its overridden PATH. The Desktop lease is
            # already scoped to the managed binary, so execute that exact path
            # for every use-time InfraGuard command as well.
            managed_path = os.environ["IAC_CODE_DESKTOP_INFRAGUARD_PATH"]
            managed_command = [managed_path, *command[1:]]
        return (
            await _run_infraguard_command(
                managed_command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=env,
            ),
            None,
        )
    finally:
        if lease is not None:
            await asyncio.to_thread(lease.__exit__, None, None, None)


def _extract_findings(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    top_level_findings = parsed.get("findings")
    if isinstance(top_level_findings, list):
        findings.extend(finding for finding in top_level_findings if isinstance(finding, dict))

    top_level_violations = parsed.get("violations")
    if isinstance(top_level_violations, list):
        findings.extend(violation for violation in top_level_violations if isinstance(violation, dict))

    raw_results = parsed.get("results")
    if not isinstance(raw_results, list):
        return findings

    for finding in raw_results:
        if not isinstance(finding, dict):
            continue
        if "violations" not in finding:
            findings.append(finding)
            continue
        violations = finding.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            normalized = dict(violation)
            if "file" not in normalized and isinstance(finding.get("file"), str):
                normalized["file"] = finding["file"]
            findings.append(normalized)
    return findings


def _looks_like_scan_payload(parsed: dict[str, Any]) -> bool:
    return any(key in parsed for key in ("results", "findings", "violations", "summary"))


def _summary_blocking_findings(summary: dict[str, Any], blocking_severities: set[str]) -> int:
    severity_counts = summary.get("severity_counts")
    candidates = [summary]
    if isinstance(severity_counts, dict):
        candidates.append(severity_counts)

    max_count = 0
    for candidate in candidates:
        total = 0
        for severity in blocking_severities:
            value = candidate.get(severity)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                total += value
                continue
            if isinstance(value, str):
                try:
                    total += int(value)
                except ValueError:
                    continue
        max_count = max(max_count, total)
    return max_count


def _summary_reports_findings(summary: dict[str, Any]) -> bool:
    severity_counts = summary.get("severity_counts")
    if isinstance(severity_counts, dict):
        for value in severity_counts.values():
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value > 0:
                return True
            if isinstance(value, str):
                try:
                    if int(value) > 0:
                        return True
                except ValueError:
                    continue

    for key in ("total", "total_violations", "violations", "files_with_violations"):
        value = summary.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return True
        if isinstance(value, str):
            try:
                if int(value) > 0:
                    return True
            except ValueError:
                continue
    return False


def _scan_file_path(file_path: str, cwd: str) -> Path:
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


def _file_sha256(path: Path) -> str | None:
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _read_file_content(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", newline="") as file:
            return file.read()
    except OSError:
        return None
    except UnicodeDecodeError:
        return None


def _list_of_strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def _unsupported_no_waivers_flag(completed: subprocess.CompletedProcess[str]) -> bool:
    stderr = (completed.stderr or "").lower()
    unsupported_phrases = ("unknown flag", "unknown option", "unrecognized flag", "unrecognized option")
    return (
        completed.returncode != 0
        and "--no-waivers" in stderr
        and any(phrase in stderr for phrase in unsupported_phrases)
    )


async def _run_infraguard_command(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = await create_subprocess_exec(
        *guarded_command(command, kind="infraguard"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=spawn_env(_environment_with_overrides(env)),
        **popen_kwargs,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        stdout_bytes, stderr_bytes = await _terminate_infraguard_process(process)
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=_decode_process_output(stdout_bytes),
            stderr=_decode_process_output(stderr_bytes),
        ) from exc
    except asyncio.CancelledError:
        await _terminate_infraguard_process(process)
        raise

    return subprocess.CompletedProcess(
        command,
        process.returncode if process.returncode is not None else 0,
        stdout=_decode_process_output(stdout_bytes),
        stderr=_decode_process_output(stderr_bytes),
    )


def _environment_with_overrides(env_overrides: dict[str, str] | None) -> dict[str, str] | None:
    if not env_overrides:
        return None
    env = os.environ.copy()
    env.update(env_overrides)
    return env


async def _terminate_infraguard_process(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    await _terminate_infraguard_process_tree(process)
    try:
        return await asyncio.wait_for(process.communicate(), timeout=3)
    except asyncio.TimeoutError:
        await _terminate_infraguard_process_tree(process, force=True)
        with suppress(Exception):
            return await asyncio.wait_for(process.communicate(), timeout=3)
    return b"", b""


async def _terminate_infraguard_process_tree(
    process: asyncio.subprocess.Process,
    *,
    force: bool = False,
) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        await _terminate_windows_process_tree(process.pid, force=force)
        return
    from iac_code.desktop.external_env import is_guardian_process

    if is_guardian_process(process):
        if force:
            process.kill()
        else:
            process.terminate()
        await asyncio.shield(process.wait())
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        if force:
            process.kill()
        else:
            process.terminate()


async def _terminate_windows_process_tree(pid: int, *, force: bool = False) -> None:
    command = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        command.insert(1, "/F")
    try:
        taskkill = await create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.wait_for(taskkill.wait(), timeout=2)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        return


def _decode_process_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return output.decode("utf-8", errors="replace")


def _expand_policies(tool_input: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    policies = _list_of_strings(tool_input.get("policies"))
    selected_aspects = _list_of_strings(tool_input.get("selected_aspects"))
    aspect_policy_map = tool_input.get("aspect_policy_map")
    unknown_aspects: list[str] = []

    if selected_aspects:
        if not isinstance(aspect_policy_map, dict):
            return policies, selected_aspects, selected_aspects
        for aspect_key in selected_aspects:
            raw_aspect = aspect_policy_map.get(aspect_key)
            if not isinstance(raw_aspect, dict):
                unknown_aspects.append(aspect_key)
                continue
            policies.extend(_list_of_strings(raw_aspect.get("policies")))

    deduped_policies: list[str] = []
    seen: set[str] = set()
    for policy in policies:
        if policy in seen:
            continue
        seen.add(policy)
        deduped_policies.append(policy)
    return deduped_policies, selected_aspects, unknown_aspects


def _json_object(output: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _severity_counts(payload: dict[str, Any]) -> dict[str, int]:
    summary = payload.get("summary")
    candidates: list[dict[str, Any]] = []
    if isinstance(summary, dict):
        severity_counts = summary.get("severity_counts")
        if isinstance(severity_counts, dict):
            candidates.append(severity_counts)
        candidates.append(summary)

    counts: dict[str, int] = {}
    for candidate in candidates:
        for severity in ("critical", "high", "medium", "low"):
            value = _int_value(candidate.get(severity))
            if value is not None:
                counts[severity] = max(counts.get(severity, 0), value)

    findings = payload.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "").strip().lower()
            if severity:
                counts[severity] = max(counts.get(severity, 0), 0)
    return counts


def _severity_counts_text(counts: dict[str, int]) -> str:
    preferred = {"critical", "high", "medium", "low"}
    ordered = [
        "{} {}".format(severity, counts[severity])
        for severity in ("critical", "high", "medium", "low")
        if severity in counts
    ]
    extra = ["{} {}".format(key, value) for key, value in sorted(counts.items()) if key not in preferred]
    return ", ".join([*ordered, *extra])


def _finding_count(payload: dict[str, Any], counts: dict[str, int]) -> int:
    findings = payload.get("findings")
    if isinstance(findings, list):
        return len([finding for finding in findings if isinstance(finding, dict)])
    summary = payload.get("summary")
    if isinstance(summary, dict):
        for key in ("total_violations", "total", "violations"):
            value = _int_value(summary.get(key))
            if value is not None:
                return value
    return sum(counts.values())


def _status_text(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return _("error")
    if "passed" in payload:
        return _("passed") if payload.get("passed") else _("failed")
    return _("completed")


def _error_label(error: Any) -> str:
    labels = {
        "unsupported_no_waivers_flag": _("InfraGuard CLI does not support --no-waivers"),
    }
    key = str(error)
    return labels.get(key, key)


def _plural_findings(count: int) -> str:
    if count == 1:
        return _("1 finding")
    return _("{count} findings").format(count=count)


def _join_lines(lines: list[str]) -> str:
    return "\n     ".join(line for line in lines if line)


def _short_file_path(payload: dict[str, Any]) -> str:
    return str(payload.get("file_path") or payload.get("canonical_file_path") or "")


def _format_command(command: Any) -> str:
    if not isinstance(command, list):
        return ""
    return shlex.join(str(part) for part in command)


def _list_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return ""


def _format_summary(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ("total_violations", "files_scanned", "files_with_violations", "waived_count", "expired_waiver_count"):
        value = summary.get(key)
        if value is not None:
            lines.append("  {}: {}".format(key, value))
    return lines


def _finding_title(finding: dict[str, Any]) -> str:
    severity = str(finding.get("severity") or "unknown")
    rule = str(finding.get("rule_id") or finding.get("rule") or finding.get("id") or "unknown")
    resource = str(finding.get("resource_id") or finding.get("resource") or "")
    line = finding.get("line")
    pieces = [severity, rule]
    if resource:
        pieces.append(resource)
    if line is not None:
        pieces.append(_("line {line}").format(line=line))
    return " · ".join(pieces)


def _format_finding_lines(finding: dict[str, Any]) -> list[str]:
    lines = ["- {}".format(_finding_title(finding))]
    reason = finding.get("reason")
    if reason:
        lines.append("  " + _("Reason: {reason}").format(reason=reason))
    recommendation = finding.get("recommendation")
    if recommendation:
        lines.append("  " + _("Recommendation: {recommendation}").format(recommendation=recommendation))
    snippet = finding.get("snippet")
    if snippet:
        lines.append("  " + _("Snippet: {snippet}").format(snippet=snippet))
    return lines


def _render_infraguard_compact(payload: dict[str, Any]) -> str:
    file_path = _short_file_path(payload)
    if payload.get("error"):
        parts = [_("error"), _error_label(payload.get("error"))]
        if file_path:
            parts.append(file_path)
        return " · ".join(parts)

    counts = _severity_counts(payload)
    finding_count = _finding_count(payload, counts)
    parts = [
        _status_text(payload),
        _plural_findings(finding_count),
        _("blocking {count}").format(count=payload.get("blocking_findings", 0)),
    ]
    counts_text = _severity_counts_text(counts)
    if counts_text:
        parts.append(counts_text)
    if file_path:
        parts.append(file_path)
    return " · ".join(parts)


def _render_infraguard_verbose(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    command = _format_command(payload.get("command"))
    if command:
        lines.append(_("Command: {command}").format(command=command))
    lines.append(_("Status: {status}").format(status=_status_text(payload)))
    if payload.get("error"):
        lines.append(_("Error: {error}").format(error=_error_label(payload.get("error"))))
    file_path = _short_file_path(payload)
    if file_path:
        lines.append(_("File: {file_path}").format(file_path=file_path))
    if payload.get("mode") is not None:
        lines.append(_("Mode: {mode}").format(mode=payload.get("mode")))
    if payload.get("exit_code") is not None:
        lines.append(_("Exit code: {exit_code}").format(exit_code=payload.get("exit_code")))
    if payload.get("ignore_waivers") is not None:
        lines.append(_("Ignore waivers: {value}").format(value=payload.get("ignore_waivers")))
    blocking_severities = _list_text(payload.get("blocking_severities"))
    if blocking_severities:
        lines.append(_("Blocking severities: {severities}").format(severities=blocking_severities))
    if payload.get("blocking_findings") is not None:
        lines.append(_("Blocking findings: {count}").format(count=payload.get("blocking_findings")))
    selected_aspects = _list_text(payload.get("selected_aspects"))
    if selected_aspects:
        lines.append(_("Aspects: {aspects}").format(aspects=selected_aspects))

    policies = [str(policy) for policy in payload.get("expanded_policies") or [] if policy]
    if policies:
        lines.append(_("Policies:"))
        lines.extend("  - {}".format(policy) for policy in policies)

    summary = payload.get("summary")
    counts = _severity_counts(payload)
    if isinstance(summary, dict) or counts:
        lines.append(_("Summary:"))
        if counts:
            lines.append("  " + _("Severity counts: {counts}").format(counts=_severity_counts_text(counts)))
        if isinstance(summary, dict):
            lines.extend(_format_summary(summary))

    stderr = payload.get("stderr")
    if stderr:
        lines.append(_("Stderr: {stderr}").format(stderr=stderr))

    findings = [finding for finding in payload.get("findings") or [] if isinstance(finding, dict)]
    lines.append(_("Findings:"))
    if findings:
        for finding in findings:
            lines.extend(_format_finding_lines(finding))
    else:
        lines.append("  " + _("No findings."))
    return _join_lines(lines)


class InfraGuardScanTool(Tool):
    """Run InfraGuard and normalize scan results for deterministic completion guards."""

    def __init__(self, *, step_config: dict[str, Any] | None = None) -> None:
        raw_infraguard = (step_config or {}).get("infraguard") if isinstance(step_config, dict) else None
        self._configured_infraguard = dict(raw_infraguard) if isinstance(raw_infraguard, dict) else {}

    @property
    def name(self) -> str:
        return "infraguard_scan"

    @property
    def description(self) -> str:
        return _("Run InfraGuard static scan and return structured JSON results.")

    @property
    def timeout(self) -> float | None:
        return _SCAN_TIMEOUT_SECONDS + _TOOL_TIMEOUT_BUFFER_SECONDS

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["file_path"],
            "properties": {
                "file_path": {"type": "string"},
                "mode": {"type": "string", "enum": ["static", "preview"], "default": "static"},
                "policies": {"type": "array", "items": {"type": "string"}},
                "selected_aspects": {"type": "array", "items": {"type": "string"}},
                "aspect_policy_map": {"type": "object"},
                "ignore_waivers": {"type": "boolean", "default": True},
                "blocking_severities": {"type": "array", "items": {"type": "string"}, "default": ["high"]},
                "include_file_content": {"type": "boolean", "default": False},
            },
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    @property
    def render_verbose_result_in_transcript(self) -> bool:
        return True

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        payload = _json_object(output)
        if payload is None:
            return None
        return _render_infraguard_verbose(payload) if verbose else _render_infraguard_compact(payload)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        tool_input = self._apply_configured_infraguard_defaults(tool_input)
        file_path = str(tool_input["file_path"])
        mode = str(tool_input.get("mode") or "static")
        aspect_policy_map_present = isinstance(tool_input.get("aspect_policy_map"), dict)
        selected_aspects_for_contract = _list_of_strings(tool_input.get("selected_aspects"))
        if aspect_policy_map_present and _list_of_strings(tool_input.get("policies")):
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "raw_policies_not_allowed_with_aspects",
                        "file_path": file_path,
                        "selected_aspects": selected_aspects_for_contract,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        if aspect_policy_map_present and not selected_aspects_for_contract:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "selected_aspects_required",
                        "file_path": file_path,
                        "selected_aspects": [],
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        policies, selected_aspects, unknown_aspects = _expand_policies(tool_input)
        if unknown_aspects:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "unknown_policy_aspect",
                        "unknown_aspects": unknown_aspects,
                        "selected_aspects": selected_aspects,
                        "file_path": file_path,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        command = ["infraguard", "scan", file_path, "--format", "json", "--mode", mode]

        for policy in policies:
            command.extend(["--policy", policy])
        if tool_input.get("ignore_waivers", True):
            command.append("--no-waivers")

        try:
            completed, prerequisite_status = await _run_infraguard_with_desktop_lease(
                command,
                cwd=context.cwd,
                timeout_seconds=_SCAN_TIMEOUT_SECONDS,
                env=context.env_overrides,
            )
            if prerequisite_status is not None:
                return _desktop_prerequisite_error(prerequisite_status, file_path)
            assert completed is not None
            if "--no-waivers" in command and _unsupported_no_waivers_flag(completed):
                return ToolResult(
                    content=json.dumps(
                        {
                            "error": "unsupported_no_waivers_flag",
                            "command": command,
                            "exit_code": completed.returncode,
                            "stderr": completed.stderr,
                            "file_path": file_path,
                            "selected_aspects": selected_aspects,
                            "expanded_policies": policies,
                            "ignore_waivers": True,
                        },
                        ensure_ascii=False,
                    ),
                    is_error=True,
                )
        except FileNotFoundError as error:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "command_not_found",
                        "command": command,
                        "stderr": str(error),
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        except subprocess.TimeoutExpired as error:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "timeout",
                        "command": command,
                        "stderr": str(error),
                        "timeout": error.timeout,
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        if completed.returncode not in {0, 1, 2}:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "unexpected_exit_code",
                        "command": command,
                        "exit_code": completed.returncode,
                        "stderr": completed.stderr,
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "malformed_json",
                        "command": command,
                        "exit_code": completed.returncode,
                        "stderr": completed.stderr,
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        if not isinstance(parsed, dict):
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "malformed_json",
                        "command": command,
                        "exit_code": completed.returncode,
                        "stderr": completed.stderr,
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        if "error" in parsed or not _looks_like_scan_payload(parsed):
            error_value = parsed.get("error") if "error" in parsed else "invalid_scan_payload"
            return ToolResult(
                content=json.dumps(
                    {
                        "error": str(error_value),
                        "command": command,
                        "exit_code": completed.returncode,
                        "stderr": completed.stderr,
                        "file_path": file_path,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                        "details": parsed,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        scan_path = _scan_file_path(file_path, context.cwd)
        file_sha256 = _file_sha256(scan_path)
        findings = _extract_findings(parsed)
        summary = parsed.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        blocking_severities = {
            str(severity).strip().lower() for severity in tool_input.get("blocking_severities", ["high"]) if severity
        }
        findings_blocking_count = sum(
            1
            for finding in findings
            if isinstance(finding, dict) and str(finding.get("severity", "")).strip().lower() in blocking_severities
        )
        blocking_findings = max(findings_blocking_count, _summary_blocking_findings(summary, blocking_severities))
        if (
            completed.returncode == 2
            and blocking_findings == 0
            and not findings
            and not _summary_reports_findings(summary)
        ):
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "inconsistent_scan_payload",
                        "command": command,
                        "exit_code": completed.returncode,
                        "stderr": completed.stderr,
                        "file_path": file_path,
                        "summary": summary,
                        "findings": findings,
                        "selected_aspects": selected_aspects,
                        "expanded_policies": policies,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        payload = {
            "command": command,
            "exit_code": completed.returncode,
            "mode": mode,
            "ignore_waivers": bool(tool_input.get("ignore_waivers", True)),
            "blocking_severities": list(tool_input.get("blocking_severities", ["high"])),
            "passed": blocking_findings == 0,
            "blocking_findings": blocking_findings,
            "findings": findings,
            "summary": summary,
            "file_path": file_path,
            "canonical_file_path": str(scan_path),
            "file_sha256": file_sha256,
            "selected_aspects": selected_aspects,
            "expanded_policies": policies,
        }
        if tool_input.get("include_file_content", False):
            payload["file_content"] = _read_file_content(scan_path)
        return ToolResult(content=json.dumps(payload, ensure_ascii=False), is_error=False)

    def _apply_configured_infraguard_defaults(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        if not self._configured_infraguard:
            return dict(tool_input)
        resolved = dict(tool_input)
        for key in ("mode", "ignore_waivers", "blocking_severities"):
            if key in self._configured_infraguard:
                resolved[key] = self._configured_infraguard[key]
        aspects = self._configured_infraguard.get("aspects")
        if isinstance(aspects, dict):
            resolved["aspect_policy_map"] = aspects
        return resolved
