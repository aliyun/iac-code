import asyncio
from pathlib import Path

import pytest

import iac_code.i18n as i18n

_ZH_WEBUI_MO = Path(i18n.__file__).parent / "locales" / "zh" / "LC_MESSAGES" / "webui.mo"


def test_set_language_rebinds_current_language():
    i18n.set_language("es")
    assert i18n.get_current_language() == "es"
    i18n.set_language("en")
    assert i18n.get_current_language() == "en"


def test_resolve_ui_language_prefers_valid_override():
    assert i18n.resolve_ui_language("ja") == "ja"


def test_resolve_ui_language_ignores_invalid_override(monkeypatch):
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert i18n.resolve_ui_language("klingon") == "en"
    assert i18n.resolve_ui_language(None) == "en"


def test_resolve_ui_language_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "de_DE")
    assert i18n.resolve_ui_language(None) == "de"


def test_load_webui_catalog_english_is_empty():
    assert i18n.load_webui_catalog("en") == {}


@pytest.mark.skipif(
    not _ZH_WEBUI_MO.exists(),
    reason="compiled webui.mo absent (run `make translate`); .mo is a build artifact",
)
def test_load_webui_catalog_zh_is_populated():
    catalog = i18n.load_webui_catalog("zh")
    assert catalog, "zh webui catalog should not be empty once compiled"
    # A stable, low-churn key that must be translated.
    assert catalog.get("Session") == "会话"


def test_display_names_cover_all_supported():
    for lang in i18n.SUPPORTED_LANGUAGES:
        assert lang in i18n.LANGUAGE_DISPLAY_NAMES


@pytest.mark.asyncio
async def test_request_language_is_task_local_and_restores_process_language(monkeypatch):
    i18n.set_language("en")
    monkeypatch.setattr(
        i18n,
        "translate_message",
        lambda message, *, language: f"{language}:{message}",
    )
    monkeypatch.setattr(
        i18n,
        "translate_plural",
        lambda singular, plural, n, *, language: f"{language}:{singular if n == 1 else plural}",
    )

    async def translated(language: str) -> tuple[str, str, str]:
        with i18n.use_request_language(language):
            await asyncio.sleep(0)
            return i18n.get_current_language(), i18n._("Hello"), i18n.ngettext("item", "items", 2)

    assert await asyncio.gather(translated("zh"), translated("ja")) == [
        ("zh", "zh:Hello", "zh:items"),
        ("ja", "ja:Hello", "ja:items"),
    ]
    assert i18n.get_current_language() == "en"
    assert i18n._("Hello") == "Hello"
