import pytest

import iac_code.pipeline.engine.architecture_semantic_planning as asp


class _FakeResponse:
    text = '{"node_labels": [], "edges": [], "views": []}'
    usage = None


@pytest.mark.asyncio
async def test_create_semantic_plan_with_llm_threads_provider_overrides(monkeypatch):
    captured: dict = {}

    class _FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def complete(self, messages, system, *, max_tokens):
            return _FakeResponse()

    monkeypatch.setattr(asp, "ProviderManager", _FakeManager)
    # Guard: default path must NOT be taken when credentials_override is given.
    monkeypatch.setattr(asp, "load_credentials", lambda *, model: pytest.fail("should not call"))

    await asp.create_semantic_plan_with_llm(
        {"visible_nodes": []},
        model="qwen3.6-plus",
        max_tokens=1000,
        user_prompt="x",
        credentials_override={"qwenpaw": "k"},
        provider_key_override="qwenpaw",
        base_url_override="http://x",
        provider_config_override={"apiBase": "http://x"},
        ignore_llm_source=True,
    )

    assert captured["credentials"] == {"qwenpaw": "k"}
    assert captured["provider_key_override"] == "qwenpaw"
    assert captured["base_url_override"] == "http://x"
    assert captured["provider_config_override"] == {"apiBase": "http://x"}
    assert captured["ignore_llm_source"] is True


@pytest.mark.asyncio
async def test_create_semantic_plan_with_llm_defaults_unchanged(monkeypatch):
    captured: dict = {}

    class _FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def complete(self, messages, system, *, max_tokens):
            return _FakeResponse()

    monkeypatch.setattr(asp, "ProviderManager", _FakeManager)
    monkeypatch.setattr(asp, "load_credentials", lambda *, model: {"default": "dk"})

    await asp.create_semantic_plan_with_llm(
        {"visible_nodes": []}, model="m", max_tokens=100, user_prompt="x"
    )

    assert captured["credentials"] == {"default": "dk"}
    assert captured["provider_key_override"] is None
    assert captured["base_url_override"] is None
    assert captured["provider_config_override"] is None
    assert captured["ignore_llm_source"] is False
