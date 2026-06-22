import httpx

from iac_code.services.capabilities.auto_detect import (
    AutoDetectCache,
    probe_openai_compatible,
)


def test_probe_returns_true_when_modalities_include_image():
    payload = {
        "data": [
            {
                "id": "custom-vl",
                "architecture": {"input_modalities": ["text", "image"]},
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        result = probe_openai_compatible(
            base_url="https://example.com/v1",
            api_key="x",
            model="custom-vl",
            client=client,
        )
    assert result is True


def test_probe_returns_none_on_unknown_schema():
    payload = {"data": [{"id": "custom-vl"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        result = probe_openai_compatible(
            base_url="https://example.com/v1",
            api_key="x",
            model="custom-vl",
            client=client,
        )
    assert result is None


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "iac_code.services.capabilities.auto_detect._cache_path",
        lambda: tmp_path / ".multimodal-cache.yml",
    )
    cache = AutoDetectCache()
    cache.set("https://x/v1", "custom-vl", True)
    cache.flush()

    fresh = AutoDetectCache()
    assert fresh.get("https://x/v1", "custom-vl") is True
    assert fresh.get("https://x/v1", "other") is None


def test_cache_flush_leaves_no_partial_temp_files(tmp_path, monkeypatch):
    """Atomic write must rename via os.replace, not leave .tmp residue behind."""
    monkeypatch.setattr(
        "iac_code.services.capabilities.auto_detect._cache_path",
        lambda: tmp_path / ".multimodal-cache.yml",
    )
    cache = AutoDetectCache()
    cache.set("https://x/v1", "m", True)
    cache.flush()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".multimodal-cache.")]
    # Only the final file should remain — no .tmp residue.
    assert leftovers == [".multimodal-cache.yml"], leftovers


def test_cache_flush_last_writer_wins_with_valid_yaml(tmp_path, monkeypatch):
    """Two sequential flushes (simulating two REPL writes) leave a parseable file."""
    monkeypatch.setattr(
        "iac_code.services.capabilities.auto_detect._cache_path",
        lambda: tmp_path / ".multimodal-cache.yml",
    )
    a = AutoDetectCache()
    a.set("https://x/v1", "m1", True)
    a.flush()

    b = AutoDetectCache()
    b.set("https://x/v1", "m2", False)
    b.flush()

    fresh = AutoDetectCache()
    # b read a's snapshot before setting m2, so both end up persisted.
    assert fresh.get("https://x/v1", "m1") is True
    assert fresh.get("https://x/v1", "m2") is False
