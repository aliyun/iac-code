"""Guard the web-session i18n catalogs against regressions.

Reads the committed ``.po`` sources (never the git-ignored compiled ``.mo``)
via babel and asserts, for every translated language, that the web-facing
catalogs are complete and placeholder-safe:

* ``webui`` domain — the entire catalog is ours (client JS + HTML). Every
  entry must be translated (non-empty, non-fuzzy) and every ``{brace}`` /
  ``{}`` placeholder in the source must survive verbatim into the translation.
* ``messages`` domain — only the entries whose source location is under
  ``src/iac_code/web/`` are in scope for the web feature. The rest of that
  catalog (1500+ REPL strings) has legitimate pre-existing gaps and is NOT
  asserted here.

Placeholder integrity matters because babel registers no brace-format checker,
so ``pybabel compile`` cannot catch a dropped/renamed ``{token}`` — this test
is the only gate for it.
"""

import re
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

import iac_code.i18n as i18n

_LOCALES_DIR = Path(i18n.__file__).parent / "locales"
# English is the source/base language and ships an empty catalog by design.
TRANSLATED_LANGUAGES = [lang for lang in i18n.SUPPORTED_LANGUAGES if lang != "en"]

_BRACE = re.compile(r"\{[^}]*\}")
_WEB_LOCATION_PREFIX = "src/iac_code/web/"


def _po_path(lang: str, domain: str) -> Path:
    return _LOCALES_DIR / lang / "LC_MESSAGES" / f"{domain}.po"


def _read(lang: str, domain: str):
    with open(_po_path(lang, domain), "rb") as fh:
        return read_po(fh)


def _brace_tokens(text: str) -> list[str]:
    """Sorted multiset of placeholder tokens, so reordering is allowed but
    adding/dropping/renaming a token is not."""
    return sorted(_BRACE.findall(text or ""))


def _is_web_located(message) -> bool:
    return any(_WEB_LOCATION_PREFIX in filename for filename, _ in message.locations)


def _webui_msgids(lang: str) -> set[str]:
    return {m.id for m in _read(lang, "webui") if m.id}


# --- webui domain -----------------------------------------------------------


def test_webui_catalog_is_nonempty():
    # A wiped catalog (0 msgids) would vacuously pass the per-entry loops below.
    assert len(_webui_msgids("zh")) > 0


def test_webui_msgid_sets_match_across_languages():
    reference = _webui_msgids(TRANSLATED_LANGUAGES[0])
    for lang in TRANSLATED_LANGUAGES[1:]:
        current = _webui_msgids(lang)
        assert current == reference, (
            f"{lang} webui msgid set diverges from {TRANSLATED_LANGUAGES[0]}: "
            f"missing={sorted(reference - current)[:5]} extra={sorted(current - reference)[:5]}"
        )


@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_webui_fully_translated(lang: str):
    empty = [m.id for m in _read(lang, "webui") if m.id and not m.string]
    fuzzy = [m.id for m in _read(lang, "webui") if m.id and m.fuzzy]
    assert not empty, f"{lang} webui has {len(empty)} untranslated entries, e.g. {empty[:5]}"
    assert not fuzzy, f"{lang} webui has {len(fuzzy)} fuzzy entries, e.g. {fuzzy[:5]}"


@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_webui_placeholder_integrity(lang: str):
    mismatches = [
        (m.id, m.string)
        for m in _read(lang, "webui")
        if m.id and m.string and _brace_tokens(m.id) != _brace_tokens(m.string)
    ]
    assert not mismatches, f"{lang} webui placeholder mismatch, e.g. {mismatches[:3]}"


# --- messages domain (web-located subset only) ------------------------------


def test_messages_web_subset_is_nonempty():
    web = [m for m in _read("zh", "messages") if m.id and _is_web_located(m)]
    assert len(web) > 0


@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_messages_web_entries_complete(lang: str):
    web = [m for m in _read(lang, "messages") if m.id and _is_web_located(m)]
    empty = [m.id for m in web if not m.string]
    fuzzy = [m.id for m in web if m.fuzzy]
    assert not empty, f"{lang} web-located messages untranslated: {empty}"
    assert not fuzzy, f"{lang} web-located messages fuzzy: {fuzzy}"


@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_messages_web_placeholder_integrity(lang: str):
    web = [m for m in _read(lang, "messages") if m.id and _is_web_located(m)]
    mismatches = [(m.id, m.string) for m in web if m.string and _brace_tokens(m.id) != _brace_tokens(m.string)]
    assert not mismatches, f"{lang} web-located messages placeholder mismatch: {mismatches}"
