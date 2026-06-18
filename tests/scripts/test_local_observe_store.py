import json

from scripts.observability.local_observe.store import ObserveStore


def test_store_appends_to_memory_and_jsonl(tmp_path):
    store = ObserveStore(data_dir=tmp_path, memory_limit=2)

    store.append_many([{"id": "r1", "kind": "log"}, {"id": "r2", "kind": "span"}])

    assert [record["id"] for record in store.records()] == ["r1", "r2"]
    lines = store.current_jsonl_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["r1", "r2"]
    assert store.health()["persistence_ok"] is True


def test_store_enforces_memory_limit_but_keeps_jsonl(tmp_path):
    store = ObserveStore(data_dir=tmp_path, memory_limit=2)

    store.append_many([{"id": "r1"}, {"id": "r2"}, {"id": "r3"}])

    assert [record["id"] for record in store.records()] == ["r2", "r3"]
    assert len(store.current_jsonl_path.read_text(encoding="utf-8").splitlines()) == 3


def test_clear_starts_new_jsonl_file(tmp_path):
    store = ObserveStore(data_dir=tmp_path, memory_limit=10)
    first_path = store.current_jsonl_path
    store.append_many([{"id": "r1"}])

    store.clear()

    assert store.records() == []
    assert store.current_jsonl_path != first_path
    assert store.current_jsonl_path.exists()
