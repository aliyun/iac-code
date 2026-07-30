"""Guard the webui JS extraction pipeline against the regex "dead zone".

babel's JavaScript lexer tracks ``/`` as either regex-literal or division but
does NOT model regex character-class ``[...]`` state. A bare ``/`` inside a
char class (e.g. ``/^[/@$!]+/``) flips the lexer's slash parity, and the drift
is *cumulative* across the file: a handful of such regexes silently desync the
lexer for hundreds of lines, swallowing every ``t()`` call in that range.

``components/composer.js`` historically had three such regexes whose combined
parity drift hid the effort/permission/scope labels from ``pybabel extract``.
The fix escapes the slash (``[\\/@$!]``) — a semantic no-op in JS — to resync
the lexer. This test reproduces that failure: if a bare ``/`` in a char class
is reintroduced, the effort-label msgids stop extracting and this test fails,
long before the missing translations reach a user as bare lowercase English.
"""

import io
from pathlib import Path

from babel.messages.extract import extract_javascript

import iac_code.i18n as i18n

# babel_webui.cfg registers the ``t`` keyword for the webui JS domain.
_WEBUI_KEYWORD = "t"
_COMPONENTS = Path(i18n.__file__).parent.parent / "web" / "static" / "js" / "components"
_COMPOSER_JS = _COMPONENTS / "composer.js"
_OUTPUT_PANEL_JS = _COMPONENTS / "output_panel.js"

# Representative msgids from inside the historical dead zone. "Minimal"/"None"
# are the two lowest effort tiers B4 added; the rest bracket the affected range.
_DEAD_ZONE_MSGIDS = {"None", "Minimal", "Low", "Medium", "High", "Very high", "Max", "Auto"}

# output_panel.js hit the same class of bug via a *different* trigger: escapeHtml's
# ``/"/g`` and ``/'/g`` — a bare quote inside a regex literal, which babel's lexer
# reads as the start of a JS string, desyncing every ``t()`` after it (the output
# panel's section labels rendered as bare English: "Resource stacks", "Template
# files", …). The fix rewrites those two regexes with unicode escapes for the
# quote characters (a semantic no-op — the same char is matched) so no bare quote
# sits inside a regex literal. These msgids all live *after* escapeHtml, so they
# only extract when the lexer stays in sync.
_OUTPUT_PANEL_MSGIDS = {
    "Optimizing",
    "Pending optimization",
    "File no longer exists",
    "Close preview",
    "Resource stacks",
    "Template files",
    "Architecture diagram",
}


def _extract_msgids(path: Path) -> set[str]:
    found: set[str] = set()
    with open(path, "rb") as fh:
        buf = io.BytesIO(fh.read())
    for _lineno, _func, messages, _comments in extract_javascript(buf, {_WEBUI_KEYWORD: None}, [], {}):
        if isinstance(messages, str):
            found.add(messages)
        elif messages:
            found.update(m for m in messages if m)
    return found


def test_composer_effort_labels_are_extractable():
    """The effort labels must survive babel JS extraction (no regex dead zone)."""
    extracted = _extract_msgids(_COMPOSER_JS)
    missing = sorted(_DEAD_ZONE_MSGIDS - extracted)
    assert not missing, (
        f"composer.js effort labels not extracted by babel: {missing}. "
        "A bare '/' in a regex character class likely desynced the JS lexer — "
        r"escape it as '\\/' to resync (see this module's docstring)."
    )


def test_output_panel_labels_are_extractable():
    """The output-panel labels must survive babel JS extraction (no regex dead zone)."""
    extracted = _extract_msgids(_OUTPUT_PANEL_JS)
    missing = sorted(_OUTPUT_PANEL_MSGIDS - extracted)
    assert not missing, (
        f"output_panel.js labels not extracted by babel: {missing}. "
        "A bare quote inside a regex literal in escapeHtml (e.g. reverting to "
        "'/\"/g') desyncs the JS lexer and swallows every t() after it — use a "
        "unicode escape for the quote char to resync (see this module's docstring)."
    )
