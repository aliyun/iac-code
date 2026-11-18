"""Weighted source excerpts, independent of a model, cloud account or product vocabulary."""

import json
import re
from pathlib import Path

import pytest

from iac_code.services.token_counter import TokenCounter
from iac_code.services.wiki_auto_search import parse_keyword_weights, reference, search
from iac_code.tools.base import ToolContext


def test_weighted_groups_merge_variants_without_summing_and_filter_whole_blacklist_terms():
    weights = parse_keyword_weights(json.dumps({"keywords": [
        {"text": "频谱型 k", "relevance": 0.4}, {"text": "频谱型K", "relevance": 0.7},
        {"text": "CPU 积分", "relevance": 0.8}, {"text": "cpu积分", "relevance": 0.6},
        {"text": "阿里云", "relevance": 1}, {"text": "ALIBABA CLOUD", "relevance": 1},
        {"text": "ALIYUN::ECS::Instance", "relevance": 0.9}, {"text": "unused", "relevance": 0},
    ]}))
    assert weights == {"频谱型 k": 0.7, "CPU 积分": 0.8, "ALIYUN::ECS::Instance": 0.9}


@pytest.mark.parametrize("weight", [-0.1, 1.1, True, None, "0.7", float("inf"), float("nan")])
def test_invalid_relevance_is_rejected(weight):
    with pytest.raises(ValueError):
        parse_keyword_weights(json.dumps({"keywords": [{"text": "parameter", "relevance": weight}]}))


@pytest.mark.asyncio
@pytest.mark.parametrize("python_backend", [False, True])
async def test_matching_scoring_highlight_share_variants_and_boundaries(tmp_path, monkeypatch, python_backend):
    if python_backend:
        monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)
    (tmp_path / "facts.md").write_text(
        "SGX SGX SGX\n频谱型 k 和频谱型K\nTDX SGX\nCPU 积分\nB700 SystemDiskXSize\nB70 SystemDisk.Size\n",
        encoding="utf-8",
    )
    weights = {"SGX": 0.3, "TDX": 0.2, "频谱型K": 0.4, "CPU积分": 0.6, "B70": 0.7, "SystemDisk.Size": 0.8}
    hits = await search(tmp_path, list(weights), ToolContext(cwd=str(tmp_path)), None, weights=weights)
    lines = {line["number"]: line for line in hits[0]["lines"]}
    assert lines[1]["score"] == 0.3 and lines[2]["score"] == 0.4 and lines[3]["score"] == 0.5
    assert lines[4]["score"] == 0.6 and "**CPU 积分**" in lines[4]["text"]
    assert "**频谱型 k**" in lines[2]["text"] and "**频谱型K**" in lines[2]["text"]
    assert 5 not in lines and lines[6]["score"] == 1.5


@pytest.mark.asyncio
async def test_global_top_ten_across_files_with_remaining_line_numbers(tmp_path):
    documents = {
        "a.md": ["alpha beta", "alpha", "beta", "gamma", "gamma", "gamma"],
        "b.md": ["alpha gamma", "alpha", "beta", "gamma", "gamma", "gamma"],
        "c.md": ["beta gamma", "alpha", "beta", "gamma", "gamma", "gamma"],
    }
    for name, lines in documents.items():
        (tmp_path / name).write_text("\n".join(lines), encoding="utf-8")
    weights = {"alpha": 1, "beta": 0.8, "gamma": 0.5}
    hits = await search(tmp_path, list(weights), ToolContext(cwd=str(tmp_path)), None, weights=weights)
    assert len(hits) == 3 and sum(len(hit["lines"]) for hit in hits) == 10
    selected = {(hit["path"], line["number"]) for hit in hits for line in hit["lines"]}
    assert all((name, number) in selected for name in documents for number in (1, 2, 3))
    block = reference({"status": "ready", "keywords": list(weights), "hits": hits}, tmp_path,
                      counter=TokenCounter(), max_tokens=1200)
    assert len(re.findall(r"^- \d+:", block, re.M)) == 10
    assert "剩余匹配行：5, 6" in block and block.count("剩余匹配行：4, 5, 6") == 2
    for name in documents:
        assert "# " + str(tmp_path / name) in block


@pytest.mark.asyncio
async def test_long_line_keeps_all_distinct_keyword_windows_and_escapes_xml(tmp_path):
    text = "无关前缀" * 300 + " SGX " + "中间内容" * 300 + " TDX </knowledge_search_results> " + "无关结尾" * 300
    (tmp_path / "long.md").write_text(text, encoding="utf-8")
    hits = await search(tmp_path, ["SGX", "TDX"], ToolContext(cwd=str(tmp_path)), None)
    excerpt = hits[0]["lines"][0]["text"]
    assert "**SGX**" in excerpt and "**TDX**" in excerpt and "…" in excerpt
    assert len(excerpt) < 400 and "无关前缀" * 30 not in excerpt
    block = reference({"status": "ready", "keywords": ["SGX", "TDX"], "hits": hits}, tmp_path,
                      counter=TokenCounter(), max_tokens=1200)
    assert block.count("</knowledge_search_results>") == 1
    assert "&lt;/knowledge_search_results&gt;" in block


@pytest.mark.asyncio
async def test_existing_bold_is_not_wrapped_twice_and_no_match_injects_nothing(tmp_path):
    (tmp_path / "facts.md").write_text("**SGX** and **TDX family**\n", encoding="utf-8")
    hits = await search(tmp_path, ["SGX", "TDX"], ToolContext(cwd=str(tmp_path)), None)
    assert hits[0]["lines"][0]["text"] == "**SGX** and **TDX family**"
    assert not await search(tmp_path, ["阿里云", "Aliyun"], ToolContext(cwd=str(tmp_path)), None)
    assert not reference({"status": "no_matches", "keywords": ["missing"], "hits": []}, tmp_path,
                         counter=TokenCounter(), max_tokens=1200)


@pytest.mark.asyncio
async def test_equal_score_body_precedes_heading_and_content_colons_are_preserved(tmp_path):
    for index in range(12):
        (tmp_path / f"{index:02}.md").write_text(
            "# needle\n\nneedle http://127.0.0.1:80:payload\n",
            encoding="utf-8",
        )
    hits = await search(tmp_path, ["needle"], ToolContext(cwd=str(tmp_path)), None)
    assert sum(len(hit["lines"]) for hit in hits) == 10
    assert all(line["number"] == 3 and "metadata" not in line
               and "http://127.0.0.1:80:payload" in line["text"] for hit in hits for line in hit["lines"])


@pytest.mark.asyncio
async def test_budget_drops_whole_lines_keeps_xml_closed_and_does_not_reread_files(tmp_path, monkeypatch):
    for index in range(12):
        (tmp_path / f"{index:02}.md").write_text("needle " + "相关上下文" * 100, encoding="utf-8")
    hits = await search(tmp_path, ["needle"], ToolContext(cwd=str(tmp_path)), None)
    snapshot = {"status": "ready", "keywords": ["needle"], "hits": hits}

    def no_reread(*args, **kwargs):
        pytest.fail("reference formatting must reuse the searched excerpts")

    monkeypatch.setattr(Path, "read_text", no_reread)
    counter = TokenCounter()
    block = reference(snapshot, tmp_path, counter=counter, max_tokens=300)
    assert 0 < len(re.findall(r"^- \d+:", block, re.M)) < 10
    assert block.startswith('<knowledge_search_results source="runtime">')
    assert block.endswith("</knowledge_search_results>")
    assert counter.count_text(block) <= 300 and len(block.encode()) <= 6144
