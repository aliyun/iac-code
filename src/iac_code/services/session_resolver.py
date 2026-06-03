"""Shared session argument resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from iac_code.services.session_index import SessionEntry, SessionIndex


class ResolutionStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS_NAME = "ambiguous_name"


@dataclass(frozen=True)
class SessionResolution:
    status: ResolutionStatus
    entry: SessionEntry | None = None
    candidates: list[SessionEntry] = field(default_factory=list)


def resolve_session_argument(index: SessionIndex, current_cwd: str, arg: str) -> SessionResolution:
    needle = arg.strip()
    if not needle:
        return SessionResolution(status=ResolutionStatus.NOT_FOUND)

    current_entries = index.list_for_cwd(current_cwd)
    entry = _exact_id(current_entries, needle)
    if entry is not None:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=entry)

    current_prefix_matches = _id_prefix_matches(current_entries, needle)
    if len(current_prefix_matches) == 1:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=current_prefix_matches[0])
    if len(current_prefix_matches) > 1:
        return SessionResolution(status=ResolutionStatus.NOT_FOUND)

    entry = _exact_name(current_entries, needle)
    if entry is not None:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=entry)

    all_entries = index.list_all_projects()
    entry = _exact_id(all_entries, needle)
    if entry is not None:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=entry)

    global_prefix_matches = _id_prefix_matches(all_entries, needle)
    if len(global_prefix_matches) == 1:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=global_prefix_matches[0])
    if len(global_prefix_matches) > 1:
        return SessionResolution(status=ResolutionStatus.NOT_FOUND)

    name_matches = [entry for entry in all_entries if entry.name == needle]
    if len(name_matches) == 1:
        return SessionResolution(status=ResolutionStatus.FOUND, entry=name_matches[0])
    if len(name_matches) > 1:
        return SessionResolution(status=ResolutionStatus.AMBIGUOUS_NAME, candidates=name_matches)

    return SessionResolution(status=ResolutionStatus.NOT_FOUND)


def _exact_id(entries: list[SessionEntry], arg: str) -> SessionEntry | None:
    return next((entry for entry in entries if entry.session_id == arg), None)


def _id_prefix_matches(entries: list[SessionEntry], arg: str) -> list[SessionEntry]:
    return [entry for entry in entries if entry.session_id.startswith(arg)]


def _exact_name(entries: list[SessionEntry], arg: str) -> SessionEntry | None:
    return next((entry for entry in entries if entry.name == arg), None)
