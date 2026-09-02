from __future__ import annotations

from pathlib import Path

import pytest
from babel.messages.pofile import read_po

_LOCALES_ROOT = Path(__file__).parents[2] / "src" / "iac_code" / "i18n" / "locales"
_AGUI_CLI_MESSAGES = frozenset(
    {
        "Unable to load the AG-UI config file.",
        "AG-UI config file must contain a YAML mapping.",
        "--port must be between 1 and 65535.",
        "--state-dir must be a string.",
        "--idle-shutdown must be a non-negative number.",
        "--a2a-url must be a string.",
        "auth_token must be a string.",
        "a2a_token must be a string.",
    }
)


@pytest.mark.parametrize("language", ["zh", "es", "fr", "de", "ja", "pt"])
def test_all_agui_user_messages_have_non_fuzzy_translations(language: str) -> None:
    path = _LOCALES_ROOT / language / "LC_MESSAGES" / "messages.po"
    with path.open("r", encoding="utf-8") as handle:
        catalog = read_po(handle)

    expected = {
        message.id
        for message in catalog
        if message.id and any(source.startswith("src/iac_code/agui/") for source, _line in message.locations)
    } | _AGUI_CLI_MESSAGES
    incomplete = sorted(
        msgid for msgid in expected if (message := catalog.get(msgid)) is None or not message.string or message.fuzzy
    )

    assert expected
    assert incomplete == []
