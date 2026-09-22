"""Guard the ``msgid`` set extracted from source against the committed catalogs.

``tests/i18n/test_catalog_completeness.py`` only inspects entries that are
*already present* in a ``.po`` file, so it cannot notice a brand-new
``_("...")`` in Python (or ``t("...")`` in JS) that never made it into the
catalogs: the source string simply has no entry to be checked, every catalog
still looks complete, and the user silently sees bare English at runtime
(gettext returns the msgid when the catalog has no entry for it).

This closes that blind spot by diffing babel's extraction of the repository
against each committed catalog, using the very same mapping files and
``--ignore-dirs`` that ``make translate`` uses, so the two cannot disagree.

Two invariants are asserted per locale and domain:

* **Presence** — every extracted msgid has an entry in the catalog. This is
  what catches a forgotten ``make translate``.
* **No fuzzy** — an entry must not carry the ``fuzzy`` flag. ``pybabel update``
  *auto-guesses* a translation from the nearest existing msgid and marks it
  fuzzy; gettext then ignores that entry entirely, so a fuzzy msgstr is
  invisible at runtime while looking translated in the file. A near-miss msgid
  is especially dangerous here because the sibling families share most of their
  wording ("Normal permission restore ..." vs "Normal resource selection
  restore ..."), so babel happily copies a translation that says *permission*
  where the code means *resource selection*.

Translation *completeness* (non-empty ``msgstr``) is deliberately NOT asserted
for the whole ``messages`` domain — see ``test_catalog_completeness.py``, which
scopes that check to the web-facing subset. This test only guarantees that the
catalogs and the source stay in sync.
"""

from functools import lru_cache
from pathlib import Path

import pytest
from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import read_po

import iac_code.i18n as i18n

# tests/i18n/test_source_catalog_sync.py -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCALES_DIR = PROJECT_ROOT / "src" / "iac_code" / "i18n" / "locales"

# Derived from the shipped language list, exactly like test_catalog_completeness.py,
# so a newly supported locale is gated automatically. English is the source
# language and intentionally ships no catalog of its own.
TRANSLATED_LANGUAGES = [lang for lang in i18n.SUPPORTED_LANGUAGES if lang != "en"]

# (domain, babel mapping file, directories excluded from extraction)
# babel_webui.cfg is run with --ignore-dirs='vendor' by `make translate`, so the
# obfuscated resource-selector bundle must be skipped here too or it would
# contribute thousands of msgids that are never meant to be translated.
_DOMAINS = [
    ("messages", "babel.cfg", ()),
    ("webui", "babel_webui.cfg", ("vendor",)),
]

# The mapping files live at the repo root and are excluded from the sdist
# (see MANIFEST.in), so they are absent when tests run against an installed
# package. This test is a source-tree consistency gate and cannot apply there.
pytestmark = pytest.mark.skipif(
    not all((PROJECT_ROOT / cfg).is_file() for _domain, cfg, _ig in _DOMAINS),
    reason="babel mapping files absent (not a source checkout)",
)


def _catalog_keys(catalog) -> set:
    """Keys of a catalog, matching babel's own singular/plural keying.

    A plural entry is keyed by its ``(singular, plural)`` tuple rather than by
    either string alone, so the extracted side and the catalog side compare
    equal without false positives.
    """
    keys = set()
    for message in catalog:
        key = message.id
        if isinstance(key, (list, tuple)):
            if any(key):
                keys.add(tuple(key))
        elif key:
            keys.add(key)
    return keys


@lru_cache(maxsize=None)
def _extracted_keys(domain: str, cfg_name: str, ignore_dirs: tuple[str, ...]) -> frozenset:
    """msgids babel extracts from the source tree, per `make translate`'s config.

    Cached because extraction walks the whole source tree (~1s per domain) while
    the assertions are parametrized across every language.
    """
    cfg_path = PROJECT_ROOT / cfg_name
    with open(cfg_path, encoding="utf-8") as fh:
        method_map, options_map = parse_mapping_cfg(fh, filename=str(cfg_path))

    if ignore_dirs:

        def directory_filter(dirname: str) -> bool:
            return not any(part in ignore_dirs for part in Path(dirname).parts)

    else:
        directory_filter = None

    keys = set()
    for _filename, _lineno, msgid, _comments, _context in extract_from_dir(
        str(PROJECT_ROOT),
        method_map=method_map,
        options_map=options_map,
        directory_filter=directory_filter,
    ):
        if isinstance(msgid, (list, tuple)):
            if any(msgid):
                keys.add(tuple(msgid))
        elif msgid:
            keys.add(msgid)
    return frozenset(keys)


def _read_catalog(lang: str, domain: str):
    po_path = _LOCALES_DIR / lang / "LC_MESSAGES" / f"{domain}.po"
    with open(po_path, "rb") as fh:
        return read_po(fh)


def _describe(key) -> str:
    return key[0] if isinstance(key, tuple) else key


@pytest.mark.parametrize(("domain", "cfg_name", "ignore_dirs"), _DOMAINS, ids=lambda v: str(v))
def test_extraction_finds_messages(domain: str, cfg_name: str, ignore_dirs: tuple[str, ...]):
    """Extraction must actually run, or the sync assertions below are vacuous."""
    extracted = _extracted_keys(domain, cfg_name, ignore_dirs)
    assert len(extracted) > 100, f"{domain}: only {len(extracted)} msgids extracted from {cfg_name}"


@pytest.mark.parametrize(("domain", "cfg_name", "ignore_dirs"), _DOMAINS, ids=lambda v: str(v))
@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_every_source_msgid_is_in_the_catalog(lang: str, domain: str, cfg_name: str, ignore_dirs: tuple[str, ...]):
    """No source msgid may be missing from a committed catalog.

    A miss means `make translate` was not run after the string was added, so
    that string renders as untranslated English for every non-English user.
    """
    extracted = _extracted_keys(domain, cfg_name, ignore_dirs)
    catalog_keys = _catalog_keys(_read_catalog(lang, domain))
    missing = sorted(_describe(key) for key in extracted - catalog_keys)
    assert not missing, (
        f"{lang} {domain}.po is missing {len(missing)} msgid(s) present in the source tree; "
        f"run `make translate` and translate them. e.g. {missing[:5]}"
    )


@pytest.mark.parametrize(("domain", "cfg_name", "ignore_dirs"), _DOMAINS, ids=lambda v: str(v))
@pytest.mark.parametrize("lang", TRANSLATED_LANGUAGES)
def test_no_fuzzy_entries(lang: str, domain: str, cfg_name: str, ignore_dirs: tuple[str, ...]):
    """A fuzzy entry is ignored by gettext, so it must not be committed.

    `pybabel update` marks an auto-guessed translation fuzzy; committing it
    leaves the string untranslated at runtime while the catalog *looks* done.
    """
    fuzzy = sorted(_describe(message.id) for message in _read_catalog(lang, domain) if message.id and message.fuzzy)
    assert not fuzzy, (
        f"{lang} {domain}.po has {len(fuzzy)} fuzzy entr{'y' if len(fuzzy) == 1 else 'ies'} "
        f"(babel auto-guesses these from similar msgids); review and clear them. e.g. {fuzzy[:5]}"
    )
