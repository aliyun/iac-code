"""Tests for the lite session index."""

from __future__ import annotations

import os
import time

import pytest

from iac_code.agent.message import Message, TextBlock, ToolResultBlock
from iac_code.services.session_index import (
    SessionIndex,
    extract_first_json_string_field,
    extract_last_json_string_field,
    read_lite_metadata,
)
from iac_code.services.session_storage import SessionStorage

# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


class TestFieldExtraction:
    def test_extract_first_simple(self):
        chunk = '{"role":"user","cwd":"/p","content":"hi"}'
        assert extract_first_json_string_field(chunk, "cwd") == "/p"

    def test_extract_last_picks_latest(self):
        chunk = '{"git_branch":"old"}\n{"git_branch":"new"}'
        assert extract_last_json_string_field(chunk, "git_branch") == "new"

    def test_extract_missing(self):
        assert extract_first_json_string_field('{"a":"b"}', "nope") is None

    def test_extract_handles_escape(self):
        chunk = r'{"text":"line1\nline2"}'
        assert extract_first_json_string_field(chunk, "text") == "line1\nline2"

    def test_extract_truncated_chunk(self):
        # Truncated mid-string — should return whatever is captured
        chunk = '{"text":"hello world wit'
        # Decoded value preserves the tail since closing quote is missing
        assert extract_first_json_string_field(chunk, "text") == "hello world wit"


# ---------------------------------------------------------------------------
# read_lite_metadata
# ---------------------------------------------------------------------------


class TestLiteMetadata:
    @pytest.fixture
    def storage(self, tmp_path):
        return SessionStorage(projects_dir=tmp_path)

    def test_extracts_cwd_and_branch_from_first_message(self, storage, tmp_path):
        cwd = "/proj/x"
        storage.append(cwd, "sx", Message(role="user", content="howdy"), git_branch="main")
        meta = read_lite_metadata(storage.session_path(cwd, "sx"))
        assert meta.cwd == cwd
        assert meta.git_branch == "main"
        assert meta.first_prompt == "howdy"

    def test_last_prompt_meta_takes_precedence(self, storage):
        cwd = "/proj/y"
        storage.append(cwd, "sy", Message(role="user", content="first one"), git_branch=None)
        storage.append_meta(cwd, "sy", {"type": "last-prompt", "last_prompt": "what user did last"})
        meta = read_lite_metadata(storage.session_path(cwd, "sy"))
        assert meta.last_prompt == "what user did last"

    def test_skips_tool_result_only_user_messages(self, storage):
        cwd = "/proj/z"
        # First user message is just a tool_result (e.g. session created via fork) —
        # extractor should skip it and find the next text message.
        storage.append(
            cwd,
            "sz",
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="result")]),
            git_branch=None,
        )
        storage.append(cwd, "sz", Message(role="assistant", content=[TextBlock(text="hi")]), git_branch=None)
        storage.append(cwd, "sz", Message(role="user", content="real prompt"), git_branch=None)
        meta = read_lite_metadata(storage.session_path(cwd, "sz"))
        assert meta.first_prompt == "real prompt"


# ---------------------------------------------------------------------------
# SessionIndex
# ---------------------------------------------------------------------------


class TestSessionIndex:
    def test_list_for_cwd_filters_by_project(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/a", "id-a", Message(role="user", content="one"), git_branch=None)
        storage.append("/b", "id-b", Message(role="user", content="two"), git_branch=None)
        index = SessionIndex(projects_dir=tmp_path)
        a_entries = index.list_for_cwd("/a")
        assert [e.session_id for e in a_entries] == ["id-a"]
        assert index.list_for_cwd("/c") == []

    def test_list_all_projects_includes_everything(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/a", "id-a", Message(role="user", content="one"), git_branch=None)
        storage.append("/b", "id-b", Message(role="user", content="two"), git_branch=None)
        index = SessionIndex(projects_dir=tmp_path)
        ids = {e.session_id for e in index.list_all_projects()}
        assert ids == {"id-a", "id-b"}

    def test_list_sorted_by_mtime_desc(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "older", Message(role="user", content="o"), git_branch=None)
        time.sleep(0.01)
        storage.append("/p", "newer", Message(role="user", content="n"), git_branch=None)
        # Force ordering deterministically.
        new_path = storage.session_path("/p", "newer")
        os.utime(new_path, (new_path.stat().st_atime, new_path.stat().st_mtime + 100))

        index = SessionIndex(projects_dir=tmp_path)
        ids = [e.session_id for e in index.list_for_cwd("/p")]
        assert ids == ["newer", "older"]

    def test_find_by_id_or_prefix_unique(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "abc-123", Message(role="user", content="x"), git_branch=None)
        index = SessionIndex(projects_dir=tmp_path)
        entry = index.find_by_id_or_prefix("abc")
        assert entry is not None and entry.session_id == "abc-123"

    def test_find_by_id_or_prefix_ambiguous_returns_none(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "abc-1", Message(role="user", content="x"), git_branch=None)
        storage.append("/p", "abc-2", Message(role="user", content="y"), git_branch=None)
        index = SessionIndex(projects_dir=tmp_path)
        # Two prefix matches — refuse to guess.
        assert index.find_by_id_or_prefix("abc") is None

    def test_find_by_id_or_prefix_exact_overrides_ambiguity(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "abc", Message(role="user", content="x"), git_branch=None)
        storage.append("/p", "abc-extra", Message(role="user", content="y"), git_branch=None)
        index = SessionIndex(projects_dir=tmp_path)
        entry = index.find_by_id_or_prefix("abc")
        assert entry is not None and entry.session_id == "abc"
