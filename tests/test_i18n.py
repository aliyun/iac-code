"""Tests for internationalization (i18n) translation completeness.

This module ensures all language translations are complete and cover
all msgid entries from the .pot template file.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

from iac_code.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent
I18N_DIR = PROJECT_ROOT / "src" / "iac_code" / "i18n"
POT_FILE = I18N_DIR / "messages.pot"
LOCALES_DIR = I18N_DIR / "locales"

MEMORY_COMMAND_MSGIDS = {
    "Usage: /memory [<name>|search <query>|delete <name>|help]",
    "Saved memories:",
    "No memories saved yet.",
    "Matching memories:",
    "No matching memories.",
    "Memory '{name}' not found.",
    "Memory '{name}' deleted.",
    "Memory manager is unavailable.",
    "View and manage persistent memories",
    "[<name>|search <query>|delete <name>|help]",
    "Search saved memories",
    "Delete a saved memory",
    "Show memory command help",
    "Saved memory",
}


def test_ngettext_default_english_plural_selection():
    from iac_code.i18n import ngettext

    assert ngettext("{n} file", "{n} files", 1).format(n=1) == "1 file"
    assert ngettext("{n} file", "{n} files", 2).format(n=2) == "2 files"


def _get_all_msgids_from_pot(pot_file: Path) -> set[str]:
    """Extract all msgids from a .pot template file.

    Skips plural forms (message.id as tuple) and empty msgids.

    Args:
        pot_file: Path to the .pot file.

    Returns:
        A set of all msgid strings that need translation.
    """
    with open(pot_file, "r", encoding="utf-8") as f:
        catalog = read_po(f)

    return {message.id for message in catalog if message.id and isinstance(message.id, str)}


def _get_all_translations_from_po(po_file: Path) -> dict[str, str]:
    """Extract msgid->msgstr mappings from a .po file.

    Fuzzy entries are treated as untranslated (empty msgstr).
    Plural forms are skipped.

    Args:
        po_file: Path to the .po file.

    Returns:
        A dictionary mapping msgid to msgstr.
    """
    with open(po_file, "r", encoding="utf-8") as f:
        catalog = read_po(f)

    result = {}
    for message in catalog:
        if not message.id or not isinstance(message.id, str):
            continue
        if "fuzzy" in message.flags:
            result[message.id] = ""  # Treat fuzzy as untranslated
        else:
            result[message.id] = message.string
    return result


def _discover_language_dirs() -> list[Path]:
    """Discover all language directories in the locales folder.

    Returns:
        A list of paths to language directories (e.g., zh, en).
    """
    if not LOCALES_DIR.exists():
        return []

    return [
        d for d in LOCALES_DIR.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != DEFAULT_LANGUAGE
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_pot_file_exists():
    """Verify that the .pot template file exists."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    assert POT_FILE.is_file(), f"POT path exists but is not a file: {POT_FILE}"


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_translation_source_references_do_not_include_line_numbers():
    """Avoid noisy PO churn from line-number-only source changes."""
    catalog_files = [POT_FILE, *LOCALES_DIR.glob("*/LC_MESSAGES/messages.po")]
    references_with_line_numbers = []

    for catalog_file in catalog_files:
        for line_number, line in enumerate(catalog_file.read_text(encoding="utf-8").splitlines(), start=1):
            has_line_number = any(reference.rsplit(":", 1)[-1].isdigit() for reference in line.split()[1:])
            if line.startswith("#:") and has_line_number:
                references_with_line_numbers.append(f"{catalog_file.relative_to(PROJECT_ROOT)}:{line_number}: {line}")

    displayed_references = references_with_line_numbers[:20]
    if len(references_with_line_numbers) > len(displayed_references):
        displayed_references.append(f"... and {len(references_with_line_numbers) - len(displayed_references)} more")

    assert not references_with_line_numbers, (
        "Translation catalogs should use file-only source references. "
        "Run: uv run pybabel extract -F babel.cfg --add-location=file -o src/iac_code/i18n/messages.pot .\n"
        + "\n".join(displayed_references)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_pot_is_up_to_date():
    """Verify .pot file is in sync with source code _() calls."""
    with tempfile.NamedTemporaryFile(suffix=".pot", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["uv", "run", "pybabel", "extract", "-F", "babel.cfg", "--add-location=file", "-o", tmp_path, "."],
            cwd=str(PROJECT_ROOT),
            check=True,
            capture_output=True,
            timeout=60,
        )

        # Parse both .pot files
        current_msgids = _get_all_msgids_from_pot(POT_FILE)
        fresh_msgids = _get_all_msgids_from_pot(Path(tmp_path))

        missing_in_pot = fresh_msgids - current_msgids
        extra_in_pot = current_msgids - fresh_msgids

        errors = []
        if missing_in_pot:
            errors.append(f"msgids in source but missing from .pot ({len(missing_in_pot)}):")
            for mid in sorted(missing_in_pot):
                errors.append(f"  - {mid!r}")
        if extra_in_pot:
            errors.append(f"msgids in .pot but not found in source ({len(extra_in_pot)}):")
            for mid in sorted(extra_in_pot):
                errors.append(f"  - {mid!r}")

        if errors:
            pytest.fail(
                "messages.pot is out of date. Run: "
                "uv run pybabel extract -F babel.cfg --add-location=file "
                "-o src/iac_code/i18n/messages.pot .\n" + "\n".join(errors)
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_all_languages_have_po_files():
    """Verify that each language directory has a valid messages.po file."""
    language_dirs = _discover_language_dirs()

    if not language_dirs:
        pytest.skip("No language directories found")

    missing_po_files = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        if not po_file.exists():
            missing_po_files.append(f"{lang_dir.name}/LC_MESSAGES/messages.po")

    assert not missing_po_files, f"Missing .po files for languages: {missing_po_files}"


def test_supported_languages_match_locale_dirs():
    """Verify supported languages are the default language plus locale directories."""
    language_dirs = _discover_language_dirs()
    locale_codes = {lang_dir.name for lang_dir in language_dirs}

    assert len(SUPPORTED_LANGUAGES) == 7
    assert set(SUPPORTED_LANGUAGES) == {DEFAULT_LANGUAGE, *locale_codes}


def test_mo_files_up_to_date():
    """Verify that .mo files are compiled and newer than their .po files.

    At runtime gettext loads the compiled .mo file, not the .po source.
    If the .mo is missing or older than the .po, translations will be
    stale or absent.
    """
    language_dirs = _discover_language_dirs()
    if not language_dirs:
        pytest.skip("No language directories found")

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        mo_file = lang_dir / "LC_MESSAGES" / "messages.mo"

        if not po_file.exists():
            continue

        compile_cmd = f"pybabel compile -d src/iac_code/i18n/locales -l {lang_dir.name}"
        if not mo_file.exists():
            errors.append(f"{lang_dir.name}: .mo file missing — run: {compile_cmd}")
            continue

        if mo_file.stat().st_mtime < po_file.stat().st_mtime:
            errors.append(f"{lang_dir.name}: .mo file is older than .po — run: {compile_cmd}")

    if errors:
        pytest.fail(".mo files are out of date. Translations will not appear at runtime.\n" + "\n".join(errors))


def test_mo_compilation_valid():
    """Verify that .po files can be compiled to .mo without errors.

    Catches issues like incompatible placeholder flags that prevent
    compilation and leave the .mo stale.
    """
    import io

    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    language_dirs = _discover_language_dirs()
    if not language_dirs:
        pytest.skip("No language directories found")

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        if not po_file.exists():
            continue

        try:
            with open(po_file, "rb") as f:
                catalog = read_po(f)
            buf = io.BytesIO()
            write_mo(buf, catalog)
        except Exception as e:
            errors.append(f"{lang_dir.name}: compilation failed — {e}")

    if errors:
        pytest.fail(".po files have compilation errors:\n" + "\n".join(errors))


def test_translation_completeness():
    """Verify all translations are complete for all languages.

    This test checks that:
    1. All msgid entries from .pot have corresponding entries in each .po file
    2. All msgstr entries are non-empty (actually translated)
    3. Fuzzy entries are treated as untranslated
    """
    # First, ensure pot file exists
    if not POT_FILE.exists():
        pytest.skip("POT file does not exist")

    # Get all msgids from template
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)

    if not pot_msgids:
        pytest.skip("No msgid entries found in POT file")

    # Discover all language directories
    language_dirs = _discover_language_dirs()

    if not language_dirs:
        pytest.skip("No language directories found")

    # Track incomplete translations per language
    all_errors: dict[str, list[str]] = {}

    for lang_dir in language_dirs:
        lang_code = lang_dir.name
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"

        # Skip if .po file doesn't exist (other test covers this)
        if not po_file.exists():
            continue

        # Get all translations for this language
        translations = _get_all_translations_from_po(po_file)

        missing_entries = []  # msgid not in .po at all
        empty_translations = []  # msgid in .po but msgstr is empty

        for msgid in sorted(pot_msgids):
            msgstr = translations.get(msgid)
            if msgstr is None:
                missing_entries.append(msgid)
            elif not msgstr.strip():
                empty_translations.append(msgid)

        errors = []
        if missing_entries:
            errors.append(f"  Missing entries ({len(missing_entries)}):")
            for mid in missing_entries:
                errors.append(f"    - {mid!r}")
        if empty_translations:
            errors.append(f"  Empty translations ({len(empty_translations)}):")
            for mid in empty_translations:
                errors.append(f"    - {mid!r}")

        if errors:
            all_errors[lang_code] = errors

    # Assert no incomplete translations
    if all_errors:
        error_messages = []
        for lang, errors in all_errors.items():
            error_messages.append(f"Language '{lang}' has incomplete translations:")
            error_messages.extend(errors)
        pytest.fail("\n".join(error_messages))


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_memory_command_translations_are_complete():
    """Verify /memory-specific strings are translated, not copied as placeholders."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = MEMORY_COMMAND_MSGIDS - pot_msgids
    assert not missing_from_pot, f"/memory msgids missing from messages.pot: {sorted(missing_from_pot)}"

    language_dirs = _discover_language_dirs()
    assert language_dirs, "No language directories found"

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        translations = _get_all_translations_from_po(po_file)
        for msgid in sorted(MEMORY_COMMAND_MSGIDS):
            msgstr = translations.get(msgid, "").strip()
            if not msgstr:
                errors.append(f"{lang_dir.name}: missing translation for {msgid!r}")
            elif msgstr == msgid:
                errors.append(f"{lang_dir.name}: untranslated placeholder for {msgid!r}")

    assert not errors, "\n".join(errors)


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_aliyun_credential_labels_are_translatable():
    """Aliyun auth menu labels come from data tables, so guard against dynamic gettext misses."""
    from iac_code.services.providers.aliyun import MODE_DISPLAY_NAMES, MODE_FIELDS

    required_msgids = set(MODE_DISPLAY_NAMES.values())
    for mode_fields in MODE_FIELDS.values():
        required_msgids.update(label for _field_name, label, _sensitive in mode_fields)

    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = sorted(required_msgids - pot_msgids)
    assert not missing_from_pot, "Aliyun credential labels missing from messages.pot: {}".format(missing_from_pot)

    missing_or_empty_by_language: dict[str, list[str]] = {}
    for lang_dir in _discover_language_dirs():
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        missing_or_empty = sorted(msgid for msgid in required_msgids if not translations.get(msgid))
        if missing_or_empty:
            missing_or_empty_by_language[lang_dir.name] = missing_or_empty

    assert not missing_or_empty_by_language, "Aliyun credential labels missing translations: {}".format(
        missing_or_empty_by_language
    )


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_session_name_error_messages_are_translated():
    """Session rename validation errors are user-facing and must not stay English-only."""
    required_msgids = {
        "Session name must match {pattern}",
        "Session name already exists in this project: {name}",
    }
    language_dirs = _discover_language_dirs()
    if not language_dirs:
        pytest.skip("No language directories found")

    untranslated: list[str] = []
    for lang_dir in language_dirs:
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        for msgid in sorted(required_msgids):
            msgstr = translations.get(msgid, "")
            if not msgstr.strip() or msgstr == msgid:
                untranslated.append(f"{lang_dir.name}: {msgid!r}")

    assert not untranslated


class TestDetectWindowsUILanguage:
    """_detect_windows_ui_language wraps GetUserDefaultLocaleName via ctypes."""

    def test_returns_two_letter_code_for_zh_cn(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        def fake_get_user_default_locale_name(buf, size):
            for i, ch in enumerate("zh-CN"):
                buf[i] = ch
            return len("zh-CN") + 1

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = fake_get_user_default_locale_name
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() == "zh"

    def test_returns_none_when_api_fails(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        def fake_get_user_default_locale_name(buf, size):
            return 0

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = fake_get_user_default_locale_name
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() is None

    def test_returns_none_on_oserror(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = MagicMock(side_effect=OSError("boom"))
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() is None


class TestDetectLanguage:
    """_detect_language env vars + Windows fallback chain."""

    def test_env_var_zh(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert _detect_language() == "zh"

    def test_env_var_unsupported_falls_through(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("LANG", "ko_KR.UTF-8")
        monkeypatch.setattr("iac_code.i18n.sys.platform", "linux")
        assert _detect_language() == "en"

    def test_windows_path_uses_kernel32(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "win32")
        monkeypatch.setattr(
            "iac_code.i18n._detect_windows_ui_language",
            lambda: "zh",
        )
        assert _detect_language() == "zh"

    def test_windows_kernel32_returns_unsupported(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "win32")
        monkeypatch.setattr(
            "iac_code.i18n._detect_windows_ui_language",
            lambda: "ko",
        )
        assert _detect_language() == "en"

    def test_all_empty_returns_default(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "linux")
        assert _detect_language() == "en"
