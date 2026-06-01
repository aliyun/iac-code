"""Tests for the FallbackStore class."""

import json
import sys

import pytest

from iac_code.services.telemetry.fallback import FallbackStore


@pytest.fixture
def store(tmp_path):
    return FallbackStore(tmp_path / "telemetry")


def test_write_creates_jsonl_file(store, tmp_path):
    path = store.write("iac_sess_abc", [{"event.name": "iac.test", "k": 1}])
    assert path.exists()
    assert path.parent == tmp_path / "telemetry"
    assert "iac_sess_abc" in path.name
    assert path.suffix == ".jsonl"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
def test_write_creates_owner_only_file(store):
    path = store.write("iac_sess_private", [{"event.name": "iac.test", "k": 1}])

    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_write_produces_one_line_per_event(store):
    events = [{"event.name": f"iac.n{i}"} for i in range(3)]
    path = store.write("iac_sess_abc", events)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["event.name"].startswith("iac.n") for line in lines)


def test_list_returns_paths_for_all_failed_batches(store):
    p1 = store.write("iac_sess_1", [{"event.name": "a"}])
    p2 = store.write("iac_sess_2", [{"event.name": "b"}])
    batches = list(store.list_pending())
    assert p1 in batches
    assert p2 in batches


def test_list_ignores_non_failed_files(store, tmp_path):
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir(parents=True, exist_ok=True)
    (tele_dir / "other.txt").write_text("noise")
    store.write("iac_sess_1", [{"event.name": "a"}])
    assert len(list(store.list_pending())) == 1


def test_remove_deletes_file(store):
    path = store.write("iac_sess_1", [{"event.name": "a"}])
    store.remove(path)
    assert not path.exists()


def test_remove_tolerant_of_missing_file(store, tmp_path):
    fake = tmp_path / "telemetry" / "missing.jsonl"
    store.remove(fake)  # must not raise


def test_read_returns_parsed_events(store):
    path = store.write("iac_sess_1", [{"event.name": "a", "k": 1}, {"event.name": "b"}])
    events = store.read(path)
    assert len(events) == 2
    assert events[0]["event.name"] == "a"


def test_read_skips_unparseable_lines(store, tmp_path):
    path = tmp_path / "telemetry" / "failed_events.iac_sess_1.abc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event.name":"ok"}\ngarbage\n{"event.name":"ok2"}\n')
    events = store.read(path)
    assert len(events) == 2
