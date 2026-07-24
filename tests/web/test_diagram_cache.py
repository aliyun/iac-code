import json
from pathlib import Path

import iac_code.web.diagram_cache as dc
from iac_code.web.diagram_cache import cache_path, read_cached, template_hash, write_cached


def _point_cache_at(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)


def test_write_then_read_views_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.diagram_cache.get_config_dir", lambda: tmp_path)
    views = [
        {"id": "overview", "title": "总览", "mermaidSource": "graph TD\n A-->B"},
        {"id": "detail_net", "title": "网络层", "mermaidSource": "graph TD\n C-->D"},
    ]
    write_cached("ctx1", 0, "TPL", views, "model-x")
    assert read_cached("ctx1", 0, "TPL") == views


def test_read_legacy_single_source_wrapped_as_one_view(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.diagram_cache.get_config_dir", lambda: tmp_path)
    thash = template_hash("TPL")
    path = cache_path("ctx1", 0, thash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "candidateIndex": 0,
                "templateHash": thash,
                "mermaidSource": "graph TD\n X",
                "model": "m",
            }
        ),
        encoding="utf-8",
    )
    assert read_cached("ctx1", 0, "TPL") == [{"id": "overview", "title": "", "mermaidSource": "graph TD\n X"}]


def test_read_template_changed_is_miss(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.diagram_cache.get_config_dir", lambda: tmp_path)
    write_cached("ctx1", 0, "TPL", [{"id": "overview", "title": "", "mermaidSource": "g"}], "m")
    assert read_cached("ctx1", 0, "OTHER") is None


def test_read_corrupt_json_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.diagram_cache.get_config_dir", lambda: tmp_path)
    thash = template_hash("TPL")
    path = cache_path("ctx1", 0, thash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_cached("ctx1", 0, "TPL") is None


def test_read_views_missing_mermaid_filtered_empty_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.web.diagram_cache.get_config_dir", lambda: tmp_path)
    thash = template_hash("TPL")
    path = cache_path("ctx1", 0, thash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"views": [{"id": "x", "title": "y"}]}), encoding="utf-8")
    assert read_cached("ctx1", 0, "TPL") is None


def test_missing_returns_none(monkeypatch, tmp_path):
    _point_cache_at(monkeypatch, tmp_path)
    assert read_cached("ctx-1", 3, "template-body") is None


def test_empty_context_id_is_noop(monkeypatch, tmp_path):
    _point_cache_at(monkeypatch, tmp_path)
    write_cached(None, 0, "t", [{"id": "overview", "title": "", "mermaidSource": "graph TD"}], "m")
    write_cached("", 0, "t", [{"id": "overview", "title": "", "mermaidSource": "graph TD"}], "m")
    assert read_cached(None, 0, "t") is None
    assert list(tmp_path.glob("diagram-cache/**/*.json")) == []


def test_unsafe_context_id_rejected(monkeypatch, tmp_path):
    _point_cache_at(monkeypatch, tmp_path)
    write_cached("../escape", 0, "t", [{"id": "overview", "title": "", "mermaidSource": "graph TD"}], "m")
    assert read_cached("../escape", 0, "t") is None
    assert list(tmp_path.glob("diagram-cache/**/*.json")) == []
