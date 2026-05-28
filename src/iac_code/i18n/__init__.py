"""Internationalization (i18n) module for iac-code.

This module provides translation capabilities using Python's standard gettext library.
"""

import gettext
import os
import sys
from pathlib import Path
from typing import Callable

# Supported languages
SUPPORTED_LANGUAGES = ["en", "zh", "es", "fr", "de", "ja", "pt"]

# Default language (English is the source language, no .po file needed)
DEFAULT_LANGUAGE = "en"

# Module-level mutable reference to the actual gettext function
# Initially set to a pass-through function that returns the original string


def _default_gettext(message: str) -> str:
    """Default pass-through translation function (before setup)."""
    return message


_gettext_func: Callable[[str], str] = _default_gettext
_current_language: str = DEFAULT_LANGUAGE


def _(message: str) -> str:
    """Translate a message string.

    Delegates to the current gettext function. This wrapper function
    remains stable after import, while the underlying translation
    function can be updated via setup_i18n().

    Args:
        message: The message string to translate.

    Returns:
        The translated message string.
    """
    return _gettext_func(message)


# Typer/Click built-in strings that need translation
# These are referenced here so pybabel can extract them into messages.pot
# They are actually used by Typer/Click's internal gettext calls
_TYPER_CLICK_STRINGS = [
    _("Options"),
    _("Commands"),
    _("Arguments"),
    _("Show this message and exit."),
    _("Install completion for the current shell."),
    _("Show completion for the current shell, to copy it or customize the installation."),
    _("default: {default}"),
    _("required"),
    _("env var: {var}"),
    _("(dynamic)"),
    _("Aborted!"),
]


def get_current_language() -> str:
    """Return the currently detected language code (e.g., 'zh', 'en')."""
    return _current_language


def _detect_language() -> str:
    """Detect system language.

    Detection priority:
    1. LANGUAGE / LC_ALL / LC_MESSAGES / LANG environment variables
    2. Windows: GetUserDefaultLocaleName via kernel32 (returns 'zh-CN', 'en-US', ...)
    3. Default to 'en'

    Returns:
        Two-letter language code (e.g., 'zh', 'en')
    """
    env_vars = ["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"]

    for var in env_vars:
        value = os.environ.get(var)
        if value:
            lang_code = value.split("_")[0].split(".")[0].lower()
            if lang_code in SUPPORTED_LANGUAGES:
                return lang_code

    if sys.platform == "win32":
        lang_code = _detect_windows_ui_language()
        if lang_code and lang_code in SUPPORTED_LANGUAGES:
            return lang_code

    return DEFAULT_LANGUAGE


def _detect_windows_ui_language() -> str | None:
    """Read the user's UI locale via kernel32.GetUserDefaultLocaleName.

    Returns the two-letter language code (e.g. 'zh' for 'zh-CN', 'en' for 'en-US')
    or None if the call fails.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        kernel32.GetUserDefaultLocaleName.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        kernel32.GetUserDefaultLocaleName.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(85)
        if kernel32.GetUserDefaultLocaleName(buf, 85) == 0:
            return None
        loc = buf.value
        if not loc:
            return None
        return loc.split("-")[0].lower()
    except (OSError, AttributeError, ValueError):
        return None


def setup_i18n() -> None:
    """Initialize internationalization.

    This function sets up gettext with the detected system language.
    It updates the module-level `_` function to use the appropriate translation.

    The locales directory is expected to be at `locales/` relative to this file.
    English is the default fallback language (source strings are in English).

    It also binds the `messages` text domain via `gettext.bindtextdomain` /
    `gettext.textdomain`, so that Typer/Click's module-level
    `from gettext import gettext as _` (which captures the function object at
    import time) can still resolve translations at call time. This avoids any
    reliance on import ordering or monkey-patching of the `gettext` module.
    """
    global _gettext_func, _current_language

    lang = _detect_language()
    _current_language = lang

    # Get the locales directory path
    locales_dir = Path(__file__).parent / "locales"

    if lang == DEFAULT_LANGUAGE:
        # For English, use a null translation (strings are already in English)
        translation = gettext.NullTranslations()
    else:
        try:
            translation = gettext.translation(
                "messages",
                localedir=str(locales_dir),
                languages=[lang],
                fallback=True,  # Fall back to source strings if translation not found
            )
        except Exception:
            # If any error occurs, fall back to null translation
            translation = gettext.NullTranslations()

    # Update the mutable reference for our own `_()` calls.
    _gettext_func = translation.gettext

    # Bind the text domain so that `gettext.gettext(msg)` -> `dgettext('messages', msg)`
    # resolves via this localedir. This is the key mechanism that lets Click's
    # captured `_` reference work without patching or import-order tricks.
    gettext.bindtextdomain("messages", str(locales_dir))
    gettext.textdomain("messages")
