"""Tests for the lite session index."""

from __future__ import annotations

import os
import time

import pytest

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, create_recalled_memory_message
from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
from iac_code.services.session_index import (
    SessionIndex,
    extract_first_json_string_field,
    extract_last_json_string_field,
    read_lite_metadata,
)
from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.types.stream_events import Usage

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

    def test_extract_truncated_preserves_literal_backslash_n_in_fallback(self):
        chunk = '{"text":"literal \\\\n tail \\'
        assert extract_first_json_string_field(chunk, "text") == "literal \\n tail \\"

    def test_extract_truncated_decodes_real_newline_escape_in_fallback(self):
        chunk = '{"text":"line1\\nline2\\'
        assert extract_first_json_string_field(chunk, "text") == "line1\nline2\\"


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

    def test_recalled_memory_last_prompt_meta_is_ignored(self, storage):
        cwd = "/proj/m"
        storage.append(cwd, "sm", Message(role="user", content="real prompt"), git_branch=None)
        storage.append_meta(
            cwd,
            "sm",
            {
                "type": "last-prompt",
                "last_prompt": (
                    "<system-reminder>\n"
                    "Relevant persistent memories recalled for this conversation:\n\n"
                    "hidden\n"
                    "</system-reminder>"
                ),
            },
        )

        meta = read_lite_metadata(storage.session_path(cwd, "sm"))

        assert meta.last_prompt is None
        assert meta.first_prompt == "real prompt"

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

    def test_skips_recalled_memory_user_messages(self, storage):
        cwd = "/proj/r"
        storage.append(
            cwd,
            "sr",
            create_recalled_memory_message("# Recalled Memory\nhidden prompt", ["topic.md"]),
            git_branch=None,
        )
        storage.append(cwd, "sr", Message(role="user", content="real prompt"), git_branch=None)

        meta = read_lite_metadata(storage.session_path(cwd, "sr"))

        assert meta.first_prompt == "real prompt"

    def test_cleanup_prompt_last_prompt_meta_is_ignored(self, storage):
        cwd = "/proj/cp-last"
        storage.append(cwd, "scp-last", Message(role="user", content="real prompt"), git_branch=None)
        storage.append_meta(
            cwd,
            "scp-last",
            {
                "type": "last-prompt",
                "last_prompt": "检测到 pipeline rollback 后仍需要清理的云资源。只有确认 DELETE_COMPLETE 才算完成。",
            },
        )

        meta = read_lite_metadata(storage.session_path(cwd, "scp-last"))

        assert meta.last_prompt is None
        assert meta.first_prompt == "real prompt"

    def test_skips_cleanup_prompt_user_messages(self, storage):
        cwd = "/proj/cp-first"
        storage.append(
            cwd,
            "scp-first",
            create_cleanup_prompt_message("cleanup hidden prompt"),
            git_branch=None,
        )
        storage.append(cwd, "scp-first", Message(role="user", content="real prompt"), git_branch=None)

        meta = read_lite_metadata(storage.session_path(cwd, "scp-first"))

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

    def test_list_all_projects_includes_legacy_sessions(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        legacy_path = storage.legacy_session_path("/legacy", "legacy-id")
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text('{"role":"user","content":"old","cwd":"/legacy"}\n', encoding="utf-8")

        index = SessionIndex(projects_dir=tmp_path)
        entries = index.list_all_projects()

        assert [(e.session_id, e.cwd, e.title) for e in entries] == [("legacy-id", "/legacy", "old")]

    def test_directory_session_metadata_name_takes_precedence(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "named", Message(role="user", content="first prompt"), git_branch=None)
        storage.rename_session("/p", "named", "deploy-prod", git_branch=None)

        entry = SessionIndex(projects_dir=tmp_path).list_for_cwd("/p")[0]

        assert entry.session_id == "named"
        assert entry.name == "deploy-prod"
        assert entry.title == "deploy-prod"
        assert entry.auto_title == "first prompt"
        assert entry.is_legacy is False

    def test_user_prompt_mentioning_cleanup_terms_is_not_hidden(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        prompt = "How do I verify DELETE_COMPLETE for deleted stacks?"
        storage.append("/p", "cleanup-terms", Message(role="user", content=prompt), git_branch=None)

        entry = SessionIndex(projects_dir=tmp_path).list_for_cwd("/p")[0]

        assert entry.title == prompt
        assert entry.auto_title == prompt

    def test_legacy_cleanup_prompt_last_prompt_meta_is_ignored(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        cwd = "/proj/cp-last-legacy"
        storage.append(cwd, "scp-last-legacy", Message(role="user", content="real prompt"), git_branch=None)
        storage.append_meta(
            cwd,
            "scp-last-legacy",
            {
                "type": "last-prompt",
                "last_prompt": "Pipeline rollback cleanup required for leftover resources.",
            },
        )

        meta = read_lite_metadata(storage.session_path(cwd, "scp-last-legacy"))

        assert meta.last_prompt is None
        assert meta.first_prompt == "real prompt"

    def test_skips_legacy_cleanup_prompt_user_messages(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        cwd = "/proj/cp-first-legacy"
        storage.append(
            cwd,
            "scp-first-legacy",
            Message(role="user", content="Rollback cleanup required for stack-123."),
            git_branch=None,
        )
        storage.append(cwd, "scp-first-legacy", Message(role="user", content="real prompt"), git_branch=None)

        meta = read_lite_metadata(storage.session_path(cwd, "scp-first-legacy"))

        assert meta.first_prompt == "real prompt"

    def test_legacy_session_still_indexed(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        legacy_path = storage.legacy_session_path("/legacy", "legacy")
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text('{"role":"user","content":"old","cwd":"/legacy"}\n', encoding="utf-8")

        entry = SessionIndex(projects_dir=tmp_path).list_for_cwd("/legacy")[0]

        assert entry.session_id == "legacy"
        assert entry.name is None
        assert entry.title == "old"
        assert entry.is_legacy is True

    def test_directory_session_ignores_stale_metadata_session_id(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        storage.append("/p", "actual", Message(role="user", content="first prompt"), git_branch=None)
        storage.rename_session("/p", "actual", "deploy-prod", git_branch=None)
        write_session_metadata(
            storage.session_dir("/p", "actual"),
            SessionMetadata(session_id="stale", name="copied-name", cwd="/p", git_branch=None),
        )

        entry = SessionIndex(projects_dir=tmp_path).list_for_cwd("/p")[0]

        assert entry.session_id == "actual"
        assert entry.name is None
        assert entry.title == "first prompt"
        assert entry.auto_title == "first prompt"
        assert entry.is_legacy is False

    def test_duplicate_legacy_and_directory_session_id_prefers_directory(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        legacy_path = storage.legacy_session_path("/p", "same")
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text('{"role":"user","content":"legacy","cwd":"/p"}\n', encoding="utf-8")

        session_dir = storage.session_dir("/p", "same")
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / SESSION_JSONL_FILENAME).write_text(
            '{"role":"user","content":"directory","cwd":"/p"}\n',
            encoding="utf-8",
        )

        entries = SessionIndex(projects_dir=tmp_path).list_for_cwd("/p")

        assert [(entry.session_id, entry.title, entry.is_legacy) for entry in entries] == [("same", "directory", False)]

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

    def test_ignores_usage_sidecars(self, tmp_path):
        storage = SessionStorage(projects_dir=tmp_path)
        usage_store = SessionUsageStore(projects_dir=tmp_path)
        storage.append("/p", "abc", Message(role="user", content="x"), git_branch=None)
        usage_store.append("/p", "abc", Usage(input_tokens=10, output_tokens=5), provider="dashscope", model="qwen")

        index = SessionIndex(projects_dir=tmp_path)

        assert [entry.session_id for entry in index.list_for_cwd("/p")] == ["abc"]
        assert {entry.session_id for entry in index.list_all_projects()} == {"abc"}
        assert index.find_by_id_or_prefix("abc").session_id == "abc"
        assert index.find_by_id_or_prefix("abc.usage") is None
