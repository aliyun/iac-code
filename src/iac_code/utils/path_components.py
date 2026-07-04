"""Path component validation helpers."""

from __future__ import annotations

_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_RESERVED_DIGIT_TRANSLATION = str.maketrans(
    {
        "\N{SUPERSCRIPT ONE}": "1",
        "\N{SUPERSCRIPT TWO}": "2",
        "\N{SUPERSCRIPT THREE}": "3",
    }
)


def is_unsafe_windows_path_component(component: str) -> bool:
    if component.endswith((" ", ".")):
        return True
    basename = component.split(".", 1)[0].translate(_WINDOWS_RESERVED_DIGIT_TRANSLATION).upper()
    return basename in _WINDOWS_RESERVED_BASENAMES
