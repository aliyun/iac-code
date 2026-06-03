"""Tests for shared session argument resolution."""

from __future__ import annotations

from iac_code.agent.message import Message
from iac_code.services.session_index import SessionIndex
from iac_code.services.session_resolver import ResolutionStatus, resolve_session_argument
from iac_code.services.session_storage import SessionStorage


def test_resolves_current_project_name(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/p", "session-a", Message(role="user", content="hello"), git_branch=None)
    storage.rename_session("/p", "session-a", "deploy-prod", git_branch=None)

    result = resolve_session_argument(SessionIndex(projects_dir=tmp_path), "/p", "deploy-prod")

    assert result.status is ResolutionStatus.FOUND
    assert result.entry is not None
    assert result.entry.session_id == "session-a"


def test_current_project_id_wins_over_cross_project_name(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/current", "deploy-prod", Message(role="user", content="current id"), git_branch=None)
    storage.append("/other", "other-id", Message(role="user", content="other name"), git_branch=None)
    storage.rename_session("/other", "other-id", "deploy-prod", git_branch=None)

    result = resolve_session_argument(SessionIndex(projects_dir=tmp_path), "/current", "deploy-prod")

    assert result.status is ResolutionStatus.FOUND
    assert result.entry is not None
    assert result.entry.session_id == "deploy-prod"
    assert result.entry.cwd == "/current"


def test_cross_project_duplicate_name_is_ambiguous(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/a", "session-a", Message(role="user", content="a"), git_branch=None)
    storage.append("/b", "session-b", Message(role="user", content="b"), git_branch=None)
    storage.rename_session("/a", "session-a", "deploy-prod", git_branch=None)
    storage.rename_session("/b", "session-b", "deploy-prod", git_branch=None)

    result = resolve_session_argument(SessionIndex(projects_dir=tmp_path), "/c", "deploy-prod")

    assert result.status is ResolutionStatus.AMBIGUOUS_NAME
    assert result.entry is None
    assert {entry.session_id for entry in result.candidates} == {"session-a", "session-b"}


def test_ambiguous_id_prefix_is_not_found(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/p", "abc-one", Message(role="user", content="one"), git_branch=None)
    storage.append("/p", "abc-two", Message(role="user", content="two"), git_branch=None)

    result = resolve_session_argument(SessionIndex(projects_dir=tmp_path), "/p", "abc")

    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.entry is None
    assert result.candidates == []


def test_ambiguous_id_prefix_is_not_resolved_as_name(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/p", "abc-one", Message(role="user", content="one"), git_branch=None)
    storage.append("/p", "abc-two", Message(role="user", content="two"), git_branch=None)
    storage.append("/p", "named", Message(role="user", content="named"), git_branch=None)
    storage.rename_session("/p", "named", "abc", git_branch=None)

    result = resolve_session_argument(SessionIndex(projects_dir=tmp_path), "/p", "abc")

    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.entry is None
    assert result.candidates == []
