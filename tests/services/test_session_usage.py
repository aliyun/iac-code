import json

from iac_code.services.session_usage import SessionUsageStore, SessionUsageTotals
from iac_code.types.stream_events import Usage

CWD = "/tmp/status-project"


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
