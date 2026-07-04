import json
from pathlib import Path

import pytest

from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    SESSION_LAYOUT_VERSION_V2,
    SESSION_METADATA_FILENAME,
    SessionMetadata,
    write_session_metadata,
)
from iac_code.services.session_usage import SessionUsageStore, SessionUsageTotals
from iac_code.types.stream_events import Usage
from iac_code.utils import project_paths

CWD = "/tmp/status-project"


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


def test_totals_adds_usage_and_tracks_record_count() -> None:
    totals = SessionUsageTotals()

    totals.add(Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=2))
    totals.add(Usage(input_tokens=7, output_tokens=1))

    assert totals.input_tokens == 17
    assert totals.output_tokens == 6
    assert totals.cache_read_input_tokens == 3
    assert totals.cache_creation_input_tokens == 2
    assert totals.total_tokens == 23
    assert totals.recorded_events == 2
    assert totals.has_recorded_usage is True


def test_all_zero_usage_is_not_recorded(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)

    recorded = store.append(CWD, "s1", Usage(), provider="dashscope", model="qwen3.7-max")

    assert recorded is False
    assert store.load(CWD, "s1").has_recorded_usage is False
    assert not store.path_for(CWD, "s1").exists()


def test_append_and_load_round_trip(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)

    assert store.append(CWD, "s2", Usage(input_tokens=12, output_tokens=3), provider="dashscope", model="qwen3.7-max")
    assert store.append(
        CWD,
        "s2",
        Usage(input_tokens=5, output_tokens=2, cache_read_input_tokens=4, cache_creation_input_tokens=1),
        provider="dashscope",
        model="qwen3.7-max",
    )

    totals = store.load(CWD, "s2")
    assert totals.input_tokens == 17
    assert totals.output_tokens == 5
    assert totals.cache_read_input_tokens == 4
    assert totals.cache_creation_input_tokens == 1
    assert totals.total_tokens == 22
    assert totals.recorded_events == 2

    lines = store.path_for(CWD, "s2").read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    assert row["type"] == "usage"
    assert row["version"] == 1
    assert row["provider"] == "dashscope"
    assert row["model"] == "qwen3.7-max"
    assert row["created_at"].endswith("Z")


def test_load_skips_corrupt_and_unrelated_rows(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)
    path = store.path_for(CWD, "s3")
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                '{"type":"usage","version":1,"input_tokens":4,"output_tokens":6,'
                '"cache_read_input_tokens":1,"cache_creation_input_tokens":0}',
                "not json",
                '{"type":"last-prompt","last_prompt":"ignored"}',
                '{"type":"usage","version":1,"input_tokens":3,"output_tokens":2}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    totals = store.load(CWD, "s3")

    assert totals.input_tokens == 7
    assert totals.output_tokens == 8
    assert totals.cache_read_input_tokens == 1
    assert totals.cache_creation_input_tokens == 0
    assert totals.total_tokens == 15
    assert totals.recorded_events == 2


def test_path_for_uses_directory_session_layout(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)

    path = store.path_for(CWD, "s4")

    assert path == tmp_path / "-tmp-status-project" / "s4" / "usage.jsonl"


def test_load_reads_new_and_legacy_sidecars(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)
    new_path = store.path_for(CWD, "s5")
    legacy_path = store.legacy_path_for(CWD, "s5")
    new_path.parent.mkdir(parents=True)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)

    new_path.write_text(
        '{"type":"usage","version":1,"input_tokens":4,"output_tokens":6,'
        '"cache_read_input_tokens":1,"cache_creation_input_tokens":0}\n',
        encoding="utf-8",
    )
    legacy_path.write_text(
        '{"type":"usage","version":1,"input_tokens":3,"output_tokens":2,'
        '"cache_read_input_tokens":5,"cache_creation_input_tokens":7}\n',
        encoding="utf-8",
    )

    totals = store.load(CWD, "s5")

    assert totals.input_tokens == 7
    assert totals.output_tokens == 8
    assert totals.cache_read_input_tokens == 6
    assert totals.cache_creation_input_tokens == 7
    assert totals.total_tokens == 15
    assert totals.recorded_events == 2


def test_usage_store_accepts_direct_path_provider(tmp_path: Path) -> None:
    path = tmp_path / "session" / "pipeline" / "transcripts" / "transcript_1" / "usage.jsonl"
    store = SessionUsageStore(path_provider=lambda _cwd, _session_id: path)

    assert store.append("/repo", "transcript_1", Usage(input_tokens=3, output_tokens=4), provider="p", model="m")

    assert path.exists()
    assert store.load("/repo", "transcript_1").total_tokens == 7


def test_usage_store_with_direct_path_provider_ignores_legacy_path(tmp_path: Path) -> None:
    direct_path = tmp_path / "transcript" / "usage.jsonl"
    legacy_store = SessionUsageStore(projects_dir=tmp_path)
    legacy_path = legacy_store.legacy_path_for("/repo", "transcript_1")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text('{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n', encoding="utf-8")
    store = SessionUsageStore(path_provider=lambda _cwd, _session_id: direct_path)

    totals = store.load("/repo", "transcript_1")

    assert totals.total_tokens == 0
    assert totals.recorded_events == 0


def test_load_skips_mismatched_directory_session_usage(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)
    path = store.path_for(CWD, "mismatched")
    session_dir = path.parent
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_JSONL_FILENAME).write_text('{"role":"user","content":"wrong"}\n', encoding="utf-8")
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="different",
            cwd=CWD,
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    path.write_text('{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n', encoding="utf-8")

    totals = store.load(CWD, "mismatched")

    assert totals.total_tokens == 0
    assert totals.recorded_events == 0


def test_append_refuses_mismatched_directory_session_usage(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)
    path = store.path_for(CWD, "mismatched-write")
    session_dir = path.parent
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_METADATA_FILENAME).write_text('{"session_id":"different"}\n', encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        store.append(CWD, "mismatched-write", Usage(input_tokens=1), provider="p", model="m")

    assert not path.exists()


def test_usage_refuses_and_ignores_symlinked_metadata(tmp_path) -> None:
    store = SessionUsageStore(projects_dir=tmp_path)
    session_id = "symlinked-metadata-usage"
    project_dir = project_paths.project_dir_candidates(CWD, tmp_path)[0]
    session_dir = project_dir / session_id
    session_dir.mkdir(parents=True)
    target = tmp_path / "outside-metadata.json"
    target.write_text(
        '{"session_id":"symlinked-metadata-usage","layout_version":2}\n',
        encoding="utf-8",
    )
    _symlink_or_skip(target, session_dir / SESSION_METADATA_FILENAME)
    usage_path = session_dir / "usage.jsonl"

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        store.append(CWD, session_id, Usage(input_tokens=1), provider="p", model="m")

    assert not usage_path.exists()
    usage_path.write_text('{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n', encoding="utf-8")
    totals = store.load(CWD, session_id)
    assert totals.total_tokens == 0
    assert totals.recorded_events == 0


def test_usage_direct_path_provider_refuses_symlinked_metadata(tmp_path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    target = tmp_path / "outside-metadata.json"
    target.write_text(
        '{"session_id":"direct-usage","layout_version":2}\n',
        encoding="utf-8",
    )
    _symlink_or_skip(target, session_dir / SESSION_METADATA_FILENAME)
    usage_path = session_dir / "usage.jsonl"
    store = SessionUsageStore(path_provider=lambda _cwd, _session_id: usage_path)

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        store.append("/repo", "direct-usage", Usage(input_tokens=1), provider="p", model="m")

    assert not usage_path.exists()
    usage_path.write_text('{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n', encoding="utf-8")
    totals = store.load("/repo", "direct-usage")
    assert totals.total_tokens == 0
    assert totals.recorded_events == 0


def test_usage_direct_path_provider_refuses_symlinked_usage_leaf(tmp_path) -> None:
    session_dir = tmp_path / "session"
    write_session_metadata(
        session_dir,
        SessionMetadata(
            session_id="direct-usage",
            cwd="/repo",
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    usage_path = session_dir / "usage.jsonl"
    outside = tmp_path / "outside-usage.jsonl"
    outside_content = '{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n'
    outside.write_text(outside_content, encoding="utf-8")
    _symlink_or_skip(outside, usage_path)
    store = SessionUsageStore(path_provider=lambda _cwd, _session_id: usage_path)

    with pytest.raises(OSError, match="symlink|reparse"):
        store.append("/repo", "direct-usage", Usage(input_tokens=1), provider="p", model="m")

    assert outside.read_text(encoding="utf-8") == outside_content
    totals = store.load("/repo", "direct-usage")
    assert totals.total_tokens == 0
    assert totals.recorded_events == 0


def test_usage_prefers_legacy_flat_over_metadata_shadow_across_project_dir_candidates(tmp_path) -> None:
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    current_project_dir, legacy_project_dir = project_paths.project_dir_candidates(cwd, tmp_path)
    legacy_project_dir.mkdir(parents=True)
    legacy_session = legacy_project_dir / "long-shadow.jsonl"
    legacy_session.write_text('{"role":"user","content":"legacy","cwd":"%s"}\n' % cwd, encoding="utf-8")
    legacy_usage = legacy_project_dir / "long-shadow.usage.jsonl"
    legacy_usage.write_text('{"type":"usage","version":1,"input_tokens":3,"output_tokens":4}\n', encoding="utf-8")
    shadow_dir = current_project_dir / "long-shadow"
    write_session_metadata(
        shadow_dir,
        SessionMetadata(
            session_id="long-shadow",
            cwd=cwd,
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    shadow_usage = shadow_dir / "usage.jsonl"
    shadow_usage.write_text('{"type":"usage","version":1,"input_tokens":50,"output_tokens":60}\n', encoding="utf-8")
    store = SessionUsageStore(projects_dir=tmp_path)

    assert store.path_for(cwd, "long-shadow") == legacy_usage
    totals = store.load(cwd, "long-shadow")

    assert totals.input_tokens == 3
    assert totals.output_tokens == 4
    assert totals.recorded_events == 1
