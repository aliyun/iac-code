"""Authentication command — interactive provider/key/model setup."""

from __future__ import annotations

import os
import sys
import threading
import unicodedata
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlparse

if TYPE_CHECKING:
    from iac_code.services.providers.aliyun import AliyunCredential

from iac_code.config import (
    _LEGACY_KEY_NAME_ALIASES,
    PARTNER_SOURCES,
    PartnerSource,
    _load_yaml,
    _save_yaml,
    get_active_provider_key,
    get_available_partner_sources,
    get_credentials_path,
    get_llm_source,
    get_provider_config,
    get_settings_path,
)
from iac_code.i18n import _
from iac_code.services.telemetry import log_event
from iac_code.services.telemetry.names import Events

_IS_WIN32 = sys.platform == "win32"


def _display_width(s: str) -> int:
    """Terminal display width (CJK chars = 2 columns)."""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


if TYPE_CHECKING:
    from iac_code.ui.repl import CommandContext


class _BackSentinel:
    """Sentinel used by full-screen flows to request one-step navigation back."""


class LLMProvider(TypedDict):
    name: str
    display_name: str
    key_name: str
    api_base: str | None
    models: list[str]
    default_model: str
    require_api_key: bool


def _classify_base_url(url: str | None) -> str:
    """Classify base URL host to one of: 'aliyun', 'openai_compat', 'deepseek', 'other', or ''."""
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if "aliyun" in host or "dashscope" in host:
        return "aliyun"
    if "deepseek" in host:
        return "deepseek"
    if "openai" in host:
        return "openai_compat"
    return "other"


def _build_providers_from_registry() -> list[LLMProvider]:
    """Build PROVIDERS list from the central registry."""
    from iac_code.providers.registry import PROVIDER_REGISTRY

    result: list[LLMProvider] = []
    for desc in PROVIDER_REGISTRY.values():
        result.append(
            LLMProvider(
                name=desc.name,
                display_name=_(desc.display_name),
                key_name=desc.key,
                api_base=desc.base_url,
                models=desc.model_ids,
                default_model=desc.default_model,
                require_api_key=desc.require_api_key,
            )
        )
    return result


PROVIDERS: list[LLMProvider] = _build_providers_from_registry()

# ── ANSI helpers ──────────────────────────────────────────────────────
_C_SEL = "\033[96m"  # bright cyan (selected)
_C_DIM = "\033[38;2;128;128;128m"  # gray (unselected / hints)
_C_RST = "\033[0m"
_C_BOLD = "\033[1m"

_BACK = _BackSentinel()
_ALIYUN_EPOCH_FIELDS = {
    "oauth_access_token_expire",
    "oauth_refresh_token_expire",
    "sts_expiration",
}


# ── Data helpers ──────────────────────────────────────────────────────


def save_llm_key(key_name: str, api_key: str) -> None:
    """Save API key to ~/.iac-code/.credentials.yml.

    When ``key_name`` is the canonical replacement of a legacy slot
    (e.g. ``dashscope`` ← ``bailian``), drop the legacy entry so the file
    has a single source of truth.
    """
    keys_path = get_credentials_path()
    keys = _load_yaml(keys_path)
    keys[key_name] = api_key
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == key_name:
            keys.pop(legacy, None)
    _save_yaml(keys_path, keys)


def save_active_provider_config(
    provider: LLMProvider | dict, model: str, effort: str | None = None, api_base: str | None = None
) -> None:
    """Persist the provider's per-provider config and mark it active."""
    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    key_name = str(provider["key_name"])

    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}

    existing = providers.get(key_name)
    entry: dict = dict(existing) if isinstance(existing, dict) else {}
    entry["name"] = provider["name"]
    entry["model"] = model
    effective_api_base = api_base if api_base is not None else provider.get("api_base")
    if effective_api_base is not None:
        entry["apiBase"] = effective_api_base
    if effort is not None:
        entry["effort"] = effort

    providers[key_name] = entry
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == key_name:
            providers.pop(legacy, None)
    config["providers"] = providers
    config["activeProvider"] = key_name
    _save_yaml(settings_path, config)


def get_configured_providers() -> list[str]:
    """Get list of providers with configured API key (slot names normalized)."""
    try:
        keys_path = get_credentials_path()
        keys = _load_yaml(keys_path)
    except Exception:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for raw_key in keys.keys():
        canonical = _LEGACY_KEY_NAME_ALIASES.get(raw_key, raw_key)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def _load_existing_key(key_name: str) -> str | None:
    """Load an existing API key for a provider, or None.

    Falls back to legacy slot names in the file (e.g. ``bailian`` for
    ``dashscope``) so existing credentials remain visible after the rename.
    """
    creds = _load_yaml(get_credentials_path())
    value = creds.get(key_name)
    if value:
        return value
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == key_name:
            legacy_value = creds.get(legacy)
            if legacy_value:
                return legacy_value
    return None


def _load_existing_api_base(key_name: str) -> str | None:
    """Load the saved API Base URL for a provider, or None."""
    value = get_provider_config(key_name).get("apiBase")
    return value if isinstance(value, str) and value else None


def _load_existing_model(key_name: str) -> str | None:
    """Load the last-used model for a provider, or None."""
    value = get_provider_config(key_name).get("model")
    return value if isinstance(value, str) and value else None


# ── Terminal UI primitives ────────────────────────────────────────────
# All operate on the alternate screen via raw stdout writes.


def _write(text: str) -> None:
    sys.stdout.write(text)


def _flush() -> None:
    sys.stdout.flush()


def _clear_screen() -> None:
    """Clear the alternate screen and move cursor to top."""
    _write("\033[H\033[2J")
    _flush()


def _render_title(title: str) -> None:
    _write(f"\n  {_C_BOLD}{title}{_C_RST}\n\n")


def _render_options(options: list[str], selected: int, hints: str) -> None:
    """Render option list + hint line."""
    for i, opt in enumerate(options):
        if i == selected:
            _write(f"  {_C_SEL}> {opt}{_C_RST}\n")
        else:
            _write(f"    {_C_DIM}{opt}{_C_RST}\n")
    _write(f"\n  {_C_DIM}{hints}{_C_RST}\n")
    _flush()


def _get_msvcrt():
    """Lazy import of msvcrt to support type checking on non-Windows platforms."""
    import msvcrt

    return msvcrt


def _drain_msvcrt_bytes() -> list[int]:
    """Block for the first byte, then drain any further bytes already in the
    msvcrt input buffer. This batches paste content (which arrives byte-by-byte)
    so multi-byte UTF-8 sequences and back-to-back keystrokes can be parsed
    together — mirroring the Unix `os.read(fd, 4096)` batch read.
    """
    msvcrt = _get_msvcrt()
    buf: list[int] = [msvcrt.getch()[0]]
    while msvcrt.kbhit():
        buf.append(msvcrt.getch()[0])
    return buf


# Extended key codes after a 0x00 / 0xE0 prefix. Only keys meaningful to auth
# input flows are mapped to events; the rest are intentionally consumed
# silently for parity with the Unix CSI parser (which also discards arrow/
# function keys it doesn't care about). Adding cursor movement etc. is
# deferred to a later phase.
_AUTH_EXT_KEY_MAP: dict[int, tuple] = {
    0x48: ("up",),
    0x50: ("down",),
    0x53: ("backspace",),  # Delete key — treated as backspace for masked input
}


def _read_input_events_win() -> list[tuple]:
    """Windows replacement for _read_input_events using msvcrt.

    Reads all currently-available bytes (drains kbhit) so paste content arrives
    as a single batch. Handles UTF-8 multi-byte chars and CRLF line endings.
    Note: msvcrt.getch() does NOT deliver Ctrl+C; the CRT raises KeyboardInterrupt
    instead — call sites convert that to a cancel event.
    """
    raw_bytes = _drain_msvcrt_bytes()

    events: list[tuple] = []
    i = 0
    while i < len(raw_bytes):
        b = raw_bytes[i]
        i += 1

        # Extended key prefix (arrows, function keys, etc.)
        if b in (0x00, 0xE0):
            if i >= len(raw_bytes):
                # Assumption: the Windows console CRT delivers extended-key
                # pairs (prefix + scancode) atomically, so kbhit() returns
                # True for the second byte while we're draining. If a prefix
                # ever arrived alone, dropping it here would cause the next
                # call to mis-parse the scancode as a printable char
                # (e.g. 0x48 → 'H'). Phase 2 task: switch to PeekConsoleInput
                # / Console Input Records so this race is structurally
                # impossible.
                break
            ext = raw_bytes[i]
            i += 1
            mapped = _AUTH_EXT_KEY_MAP.get(ext)
            if mapped is not None:
                events.append(mapped)
            # Unmapped extended keys silently consumed (Phase 1 scope)
            continue

        if b == 13:  # CR — Enter
            events.append(("enter",))
            break
        if b == 10:  # LF — Enter (or trailing half of CRLF; harmless if first)
            events.append(("enter",))
            break
        if b == 27:
            events.append(("back",))
            break
        if b == 3:
            # Defensive: msvcrt.getch() normally never returns byte 3 (Ctrl+C
            # is intercepted by the CRT and raised as KeyboardInterrupt).
            # Kept for parity with Unix and to handle any Console host that
            # routes the byte through (e.g. ENABLE_PROCESSED_INPUT off).
            events.append(("cancel",))
            break
        if b in (8, 127):
            events.append(("backspace",))
        elif b >= 0x80:
            # UTF-8 multi-byte character
            remaining = 1 if b < 0xE0 else (2 if b < 0xF0 else 3)
            end = i + remaining
            if end <= len(raw_bytes):
                try:
                    ch = bytes(raw_bytes[i - 1 : end]).decode("utf-8")
                    events.append(("char", ch))
                except UnicodeDecodeError:
                    pass
                i = end
            else:
                break  # incomplete UTF-8 at buffer end
        elif 32 <= b <= 126:
            events.append(("char", chr(b)))

    return events


def _select_read_key_win() -> tuple[str | None, str | None]:
    """Read a key for _select/_select_with_info on Windows.

    Only navigation/confirm/cancel keys are mapped — other extended keys
    return (None, None) and the caller's loop skips them. Ctrl+C is delivered
    as KeyboardInterrupt by msvcrt (caught at the call site), not as byte 3.
    """
    msvcrt = _get_msvcrt()

    c = msvcrt.getch()
    b = c[0]

    if b in (0x00, 0xE0):
        ext = msvcrt.getch()[0]
        if ext == 0x48:
            return ("up", None)
        elif ext == 0x50:
            return ("down", None)
        # Other extended keys (left/right/home/end/pageup/pagedown) are
        # intentionally ignored in selectors that only need up/down navigation.
        return (None, None)

    if b in (13, 10):
        return ("enter", None)
    if b == 27:
        return ("cancel", None)
    if b == 3:
        # Defensive — see _read_input_events_win for the same note.
        return ("cancel", None)
    return (None, None)


def _select(title: str, options: list[str], default_index: int = 0) -> int | None:
    """Full-screen selector. Returns index or None (Esc/Ctrl+C)."""
    selected = default_index
    total = len(options)
    if total == 0:
        return None
    selected = max(0, min(selected, total - 1))

    hints = "↑↓ {}  Enter {}  Esc {}".format(_("Navigate"), _("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        _render_options(options, selected, hints)

    draw()

    if _IS_WIN32:
        try:
            while True:
                action, _val = _select_read_key_win()
                if action == "enter":
                    return selected
                elif action == "cancel":
                    return None
                elif action == "up":
                    selected = (selected - 1) % total
                    draw()
                elif action == "down":
                    selected = (selected + 1) % total
                    draw()
        except (Exception, KeyboardInterrupt):
            # KeyboardInterrupt is how Ctrl+C arrives on Windows (msvcrt does
            # not deliver byte 3); treat as cancel.
            return None
    else:
        import select as select_mod
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)

        def _nb(timeout=0.05):
            r, _, _ = select_mod.select([fd], [], [], timeout)
            return os.read(fd, 1).decode("utf-8", errors="ignore") if r else None

        tty.setraw(fd)
        try:
            while True:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                if ch in ("\r", "\n"):
                    return selected
                if ch == "\x1b":
                    c2 = _nb()
                    if c2 == "[":
                        c3 = _nb()
                        if c3 == "A":
                            selected = (selected - 1) % total
                        elif c3 == "B":
                            selected = (selected + 1) % total
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                        draw()
                        tty.setraw(fd)
                    else:
                        return None
                elif ch == "\x03":
                    return None
        except Exception:
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_input_events(fd: int) -> list[tuple]:
    """Read available bytes from fd and parse into input events.

    Handles batch reads (paste) and bracketed paste escape sequences.
    Returns list of events: ('char', ch), ('backspace',), ('enter',), ('back',), ('cancel',).
    """
    if _IS_WIN32:
        return _read_input_events_win()

    import select as select_mod

    data = os.read(fd, 4096)
    if not data:
        return []

    events: list[tuple] = []
    i = 0
    while i < len(data):
        b = data[i]
        i += 1

        if b in (13, 10):
            events.append(("enter",))
            break
        elif b == 3:
            events.append(("cancel",))
            break
        elif b == 27:  # ESC
            # Check next byte to distinguish ESC key from escape sequence
            if i >= len(data):
                # ESC at end of chunk — wait briefly for more bytes
                r, _, _ = select_mod.select([fd], [], [], 0.05)
                if r:
                    data += os.read(fd, 4096)

            if i < len(data) and data[i] == ord("["):
                i += 1  # skip '['
                # Consume the full CSI sequence (params + intermediate + final byte)
                while i < len(data) and 0x30 <= data[i] <= 0x3F:
                    i += 1
                while i < len(data) and 0x20 <= data[i] <= 0x2F:
                    i += 1
                if i < len(data) and 0x40 <= data[i] <= 0x7E:
                    i += 1
                continue  # skip the entire CSI sequence (bracketed paste, arrows, etc.)
            else:
                events.append(("back",))
                break
        elif b in (127, 8):
            events.append(("backspace",))
        elif b >= 0x80:
            # Multi-byte UTF-8
            remaining_count = 1 if b < 0xE0 else (2 if b < 0xF0 else 3)
            end = i + remaining_count
            if end <= len(data):
                try:
                    ch = data[i - 1 : end].decode("utf-8")
                    events.append(("char", ch))
                except UnicodeDecodeError:
                    pass
                i = end
            # else: incomplete UTF-8 at end of chunk, skip
        else:
            ch = chr(b)
            if ch.isprintable():
                events.append(("char", ch))

    return events


def _input_masked(title: str, prompt: str, existing: str | None = None) -> str | None | _BackSentinel:
    """Full-screen masked input for API key.

    Returns str (key), None (Ctrl+C), or _BACK (Esc).
    """
    has_mask = existing is not None
    mask = "*" * len(existing) if existing else ""
    chars: list[str] = []

    if has_mask:
        hints = "Enter {}  Backspace {}  Esc {}".format(_("Keep"), _("Re-enter"), _("Back"))
    else:
        hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        display = mask if (has_mask and not chars) else ("*" * len(chars))
        _write(f"  {prompt}{display}")
        _write("\033[s")  # save cursor position (end of input)
        _write(f"\n\n  {_C_DIM}{hints}{_C_RST}")
        _write("\033[u")  # restore cursor to end of input
        _flush()

    draw()

    if _IS_WIN32:
        try:
            while True:
                events = _read_input_events_win()
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if has_mask and not chars:
                            has_mask = False
                            hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))
                        elif chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        if has_mask:
                            has_mask = False
                            hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    draw()
        except (Exception, KeyboardInterrupt):
            # Ctrl+C on Windows arrives as KeyboardInterrupt — treat as cancel.
            return None
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while True:
                events = _read_input_events(fd)
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if has_mask and not chars:
                            has_mask = False
                            hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))
                        elif chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        if has_mask:
                            has_mask = False
                            hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    draw()
                    tty.setraw(fd)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    if not chars and existing:
        return existing
    return "".join(chars) if chars else None


def _input_text(title: str, prompt: str) -> str | None | _BackSentinel:
    """Full-screen text input. Returns str, None (Ctrl+C), or _BACK (Esc)."""
    chars: list[str] = []
    hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        text = "".join(chars)
        _write(f"  {prompt}{text}")
        _write("\033[s")  # save cursor position
        _write(f"\n\n  {_C_DIM}{hints}{_C_RST}")
        _write("\033[u")  # restore cursor
        _flush()

    draw()

    if _IS_WIN32:
        try:
            while True:
                events = _read_input_events_win()
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    draw()
        except (Exception, KeyboardInterrupt):
            # Ctrl+C on Windows arrives as KeyboardInterrupt — treat as cancel.
            return None
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while True:
                events = _read_input_events(fd)
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    draw()
                    tty.setraw(fd)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return "".join(chars) if chars else None


# ── Public model selection ────────────────────────────────────────────


def select_model_interactive(
    models: list[str],
    *,
    current_model: str = "",
    provider_display_name: str = "",
) -> str | None | _BackSentinel:
    """Interactive model selection with custom model support.

    Returns model name, None (cancelled), or _BACK (Escape).
    """
    while True:
        # Build full model list, including current custom model if not already listed
        full_models = list(models)
        if current_model and current_model not in full_models:
            full_models.insert(0, current_model)

        options = []
        default_index = 0
        for i, m in enumerate(full_models):
            label = m
            if m == current_model:
                label += _(" (current)")
                default_index = i
            options.append(label)
        options.append(_("Custom model..."))

        title = (
            _("Select model for {provider}").format(provider=provider_display_name)
            if provider_display_name
            else _("Select model")
        )

        idx = _select(title, options, default_index=default_index)
        if idx is None:
            return _BACK

        if idx == len(full_models):
            result = _input_text(title, _("Enter custom model name: "))
            if result is _BACK:
                continue
            if result is None or not str(result).strip():
                continue
            return str(result).strip()

        return full_models[idx]


# ── Cloud provider definitions ────────────────────────────────────────

CLOUD_PROVIDERS = [
    {"name": "aliyun"},
]


# ── Main auth command ─────────────────────────────────────────────────


async def auth_command(context: "CommandContext | None" = None, **kwargs) -> str | None:
    """Interactive auth flow on alternate screen."""
    console = context.console if context else None
    store = context.store if context else kwargs.get("store")

    if not console:
        return _("Error: console not available")

    # Enter alternate screen
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()

    try:
        result = _auth_flow(console, store)
    finally:
        # Leave alternate screen — restores main screen cleanly
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

    # Force provider reinitialize so credential/config changes take effect
    # immediately — _on_state_change may skip reinit when only the API key
    # changed but the model and provider config stayed the same.
    if context and hasattr(context, "repl") and context.repl:
        repl = context.repl
        repl._reinitialize_provider(repl.store.get_state().model)

    return result


def _auth_flow(console, store) -> str | None:
    """Auth flow running inside alternate screen."""
    while True:
        categories = [
            _("Configure LLM Provider"),
            _("Configure IaC Cloud Service"),
        ]
        cat_idx = _select(_("Select configuration type"), categories)
        if cat_idx is None:
            return _("Auth cancelled")

        if cat_idx == 0:
            result = _llm_auth_flow(console, store)
        else:
            result = _cloud_auth_flow(console)

        if isinstance(result, _BackSentinel):
            continue
        return result


def _get_active_key_name() -> str:
    """Get the key_name of the currently active provider."""
    return get_active_provider_key() or ""


def _third_party_auth_flow(
    available_partners: list[PartnerSource],
    current_llm_source: str,
) -> str | None | _BackSentinel:
    """Second-level selection within the Third-party category."""
    candidates = list(available_partners)
    if any(ps.key == current_llm_source for ps in PARTNER_SOURCES):
        if not any(ps.key == current_llm_source for ps in candidates):
            for ps in PARTNER_SOURCES:
                if ps.key == current_llm_source:
                    candidates.append(ps)
                    break

    if len(candidates) == 0:
        return _BACK

    options: list[str] = []
    default_idx = 0
    for i, ps in enumerate(candidates):
        label = ps.display_name
        if current_llm_source == ps.key:
            label += _(" (current)")
            default_idx = i
        options.append(label)

    idx = _select(_("Select provider — {group}").format(group=_("Third-party")), options, default_index=default_idx)
    if idx is None:
        return _BACK
    partner = candidates[idx]

    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    config.pop("activeProvider", None)
    config["llm_source"] = partner.key
    _save_yaml(settings_path, config)
    return _("{status}: {provider}").format(
        status=_("Configured"),
        provider=partner.display_name,
    )


def _llm_auth_flow(console, store) -> str | None | _BackSentinel:
    """LLM provider auth flow with two-step vendor group selection."""
    active_key_name = _get_active_key_name()

    provider_groups: list[tuple[str, list[str]]] = [
        (
            "Alibaba Cloud",
            ["dashscope", "dashscope_token_plan", "aliyun_codingplan", "aliyun_codingplan_intl", "modelscope"],
        ),
        ("ZhiPu AI", ["zhipu_cn", "zhipu_intl", "zhipu_cn_codingplan", "zhipu_intl_codingplan"]),
        ("Kimi", ["kimi_cn", "kimi_intl"]),
        ("MiniMax", ["minimax_cn", "minimax_intl"]),
        ("Volcengine", ["volcengine_cn", "volcengine_cn_codingplan"]),
        ("SiliconFlow", ["siliconflow_cn", "siliconflow_intl"]),
        ("DeepSeek", ["deepseek"]),
        ("OpenAI", ["openai"]),
        ("Anthropic", ["anthropic"]),
        ("Google Gemini", ["gemini"]),
        ("Azure OpenAI", ["azure_openai"]),
        ("OpenRouter", ["openrouter"]),
        ("Local", ["ollama", "lmstudio"]),
        ("Compatible", ["openapi_compatible", "anthropic_compatible"]),
    ]

    provider_map: dict[str, LLMProvider] = {str(p["key_name"]): p for p in PROVIDERS}

    current_llm_source = get_llm_source()
    available_partners = get_available_partner_sources()
    is_current_partner = any(ps.key == current_llm_source for ps in PARTNER_SOURCES)
    show_third_party = len(available_partners) > 0 or is_current_partner

    while True:
        group_options: list[str] = []
        group_default_idx = 0

        if show_third_party:
            label = _("Third-party")
            if is_current_partner:
                label += _(" (current)")
                group_default_idx = 0
            group_options.append(label)

        for i, (group_name, keys) in enumerate(provider_groups):
            label = _(group_name)
            offset = 1 if show_third_party else 0
            if active_key_name in keys:
                label += _(" (current)")
                group_default_idx = i + offset
            group_options.append(label)

        group_idx = _select(_("Select provider"), group_options, default_index=group_default_idx)
        if group_idx is None:
            return _BACK

        if show_third_party and group_idx == 0:
            result = _third_party_auth_flow(available_partners, current_llm_source)
            if isinstance(result, _BackSentinel):
                continue
            return result

        offset = 1 if show_third_party else 0
        group_name, group_keys = provider_groups[group_idx - offset]
        group_providers = [provider_map[k] for k in group_keys if k in provider_map]

        # Step 2: Select provider within group (skip if only one)
        if len(group_providers) == 1:
            provider = group_providers[0]
        else:
            sub_options: list[str] = []
            sub_default_idx = 0
            for i, p in enumerate(group_providers):
                label = str(p["display_name"])
                if str(p["key_name"]) == active_key_name:
                    label += _(" (current)")
                    sub_default_idx = i
                sub_options.append(label)

            sub_idx = _select(
                _("Select provider — {group}").format(group=_(group_name)),
                sub_options,
                default_index=sub_default_idx,
            )
            if sub_idx is None:
                continue
            provider = group_providers[sub_idx]

        # Step 3 (Compatible providers): API Base URL
        user_api_base = None
        if provider["key_name"] in ("openapi_compatible", "anthropic_compatible"):
            existing_api_base = _load_existing_api_base(str(provider["key_name"]))
            api_base_result = _input_text_with_default(
                _("Configure {provider}").format(provider=provider["display_name"]),
                "API Base URL",
                existing_api_base or "https://",
            )
            if api_base_result is _BACK:
                continue
            if api_base_result is None:
                return _("Auth cancelled")
            user_api_base = str(api_base_result).strip()
            if not user_api_base:
                continue

        # Step 4: API key (skip for local providers that don't require one)
        if provider.get("require_api_key", True):
            existing_key = _load_existing_key(str(provider["key_name"]))
            api_key = _input_masked(
                _("Enter API key for {provider}").format(provider=provider["display_name"]),
                "API key: ",
                existing=existing_key,
            )
            if api_key is _BACK:
                continue
            if api_key is None or not str(api_key).strip():
                return _("Auth cancelled")

            api_key = str(api_key).strip()
            if api_key != existing_key:
                save_llm_key(str(provider["key_name"]), api_key)

        # Step 5: Select model
        current_model = _load_existing_model(str(provider["key_name"])) or ""
        selected = select_model_interactive(
            list(provider["models"]),
            current_model=current_model,
            provider_display_name=str(provider["display_name"]),
        )
        if selected is _BACK or selected is None:
            continue

        selected_model = str(selected)
        save_active_provider_config(provider, selected_model, api_base=user_api_base)

        log_event(
            Events.AUTH_CONFIGURED,
            {
                "provider": provider["name"],
                "has_custom_base_url": bool(user_api_base),
                "custom_base_url_host_kind": _classify_base_url(user_api_base),
            },
        )

        if store:
            store.set_state(model=selected_model)

        return _("{status}: {provider} / {model}").format(
            status=_("Configured"),
            provider=provider["display_name"],
            model=selected_model,
        )


_GROUP_NAME_MARKERS = [
    _("Third-party"),
    _("Alibaba Cloud"),
    _("ZhiPu AI"),
    _("Kimi"),
    _("MiniMax"),
    _("Volcengine"),
    _("SiliconFlow"),
    _("DeepSeek"),
    _("OpenAI"),
    _("Anthropic"),
    _("Google Gemini"),
    _("Azure OpenAI"),
    _("OpenRouter"),
    _("Local"),
    _("Compatible"),
    _("Select provider — {group}"),
]


def _cloud_provider_display(name: str) -> str:
    """Get translated display name for a cloud provider."""
    names = {
        "aliyun": _("Alibaba Cloud"),
    }
    return names.get(name, name)


def _cloud_auth_flow(console) -> str | None | _BackSentinel:
    """Cloud provider auth flow."""
    # Select cloud provider
    options = [_cloud_provider_display(p["name"]) for p in CLOUD_PROVIDERS]
    idx = _select(_("Select Cloud Provider"), options)
    if idx is None:
        return _BACK

    provider = CLOUD_PROVIDERS[idx]

    if provider["name"] == "aliyun":
        return _aliyun_auth_flow()

    return _("Auth cancelled")


def _aliyun_auth_flow() -> str | None | _BackSentinel:
    """Aliyun cloud provider auth flow with credential and region sub-menus."""
    while True:
        config_options = [
            _("Credential"),
            _("Region"),
        ]
        idx = _select(_("Configure Alibaba Cloud"), config_options)
        if idx is None:
            return _BACK

        if idx == 0:
            result = _aliyun_credential_flow()
        else:
            result = _aliyun_region_flow()

        if result is _BACK:
            continue
        return result


def _select_with_info(
    title: str,
    options: list[str],
    info_renderer: Callable[[], None] | None = None,
    default_index: int = 0,
) -> int | None:
    """Full-screen selector with optional info block between title and options.

    info_renderer: a callable that writes info lines to stdout (no clear/title).
    Returns index or None (Esc/Ctrl+C).
    """
    selected = default_index
    total = len(options)
    if total == 0:
        return None
    selected = max(0, min(selected, total - 1))

    hints = "↑↓ {}  Enter {}  Esc {}".format(_("Navigate"), _("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        if callable(info_renderer):
            info_renderer()
        _render_options(options, selected, hints)

    draw()

    if _IS_WIN32:
        try:
            while True:
                action, _val = _select_read_key_win()
                if action == "enter":
                    return selected
                elif action == "cancel":
                    return None
                elif action == "up":
                    selected = (selected - 1) % total
                    draw()
                elif action == "down":
                    selected = (selected + 1) % total
                    draw()
        except (Exception, KeyboardInterrupt):
            # Ctrl+C on Windows arrives as KeyboardInterrupt — treat as cancel.
            return None
    else:
        import select as select_mod
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)

        def _nb(timeout=0.05):
            r, _, _ = select_mod.select([fd], [], [], timeout)
            return os.read(fd, 1).decode("utf-8", errors="ignore") if r else None

        tty.setraw(fd)
        try:
            while True:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                if ch in ("\r", "\n"):
                    return selected
                if ch == "\x1b":
                    c2 = _nb()
                    if c2 == "[":
                        c3 = _nb()
                        if c3 == "A":
                            selected = (selected - 1) % total
                        elif c3 == "B":
                            selected = (selected + 1) % total
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                        draw()
                        tty.setraw(fd)
                    else:
                        return None
                elif ch == "\x03":
                    return None
        except Exception:
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render_credential_info(credential: AliyunCredential, source: str) -> None:
    """Write current credential info lines (called between title and options)."""
    from iac_code.services.providers.aliyun import MODE_FIELDS, mask_sensitive

    _write("  {}{} ({}){}\n".format(_C_DIM, _("Current configuration"), source, _C_RST))
    mode_display = _aliyun_credential_mode_label(credential.mode)
    _write("  {}{}: {}{}\n".format(_C_DIM, _("Mode"), mode_display, _C_RST))

    mode_fields = MODE_FIELDS.get(credential.mode, [])
    for field_name, label, sensitive in mode_fields:
        raw_value = getattr(credential, field_name, "")
        value = _format_aliyun_credential_field_value(field_name, raw_value, sensitive, mask_sensitive)
        display_value = value if value else _("(not set)")
        _write("  {}{}: {}{}\n".format(_C_DIM, _aliyun_credential_field_label(label), display_value, _C_RST))

    _write("  {}{}: {}{}\n".format(_C_DIM, _("Region"), credential.region_id, _C_RST))
    _write("\n")


def _aliyun_credential_mode_label(mode: str) -> str:
    if mode == "AK":
        return _("AccessKey")
    if mode == "StsToken":
        return _("STS Token")
    if mode == "RamRoleArn":
        return _("RAM Role")
    if mode == "OAuth":
        return _("OAuth Login (Browser)")
    return mode


def _aliyun_credential_field_label(label: str) -> str:
    translations = {
        "AccessKey ID": _("AccessKey ID"),
        "AccessKey Secret": _("AccessKey Secret"),
        "STS Token": _("STS Token"),
        "RAM Role ARN": _("RAM Role ARN"),
        "Session Name": _("Session Name"),
        "OAuth Site Type": _("OAuth Site Type"),
        "OAuth Access Token": _("OAuth Access Token"),
        "OAuth Refresh Token": _("OAuth Refresh Token"),
        "OAuth Access Token Expire": _("OAuth Access Token Expire"),
        "OAuth Refresh Token Expire": _("OAuth Refresh Token Expire"),
        "STS Expiration": _("STS Expiration"),
    }
    return translations.get(label, label)


def _format_aliyun_credential_field_value(
    field_name: str,
    raw_value: object,
    sensitive: bool,
    mask_sensitive: Callable[[str], str],
) -> str:
    if field_name in _ALIYUN_EPOCH_FIELDS:
        return _format_local_epoch(raw_value)

    value = str(raw_value) if raw_value not in ("", None) else ""
    if value and sensitive:
        value = mask_sensitive(value)
    return value


def _format_local_epoch(raw_value: object) -> str:
    if raw_value in ("", None):
        return ""

    if isinstance(raw_value, int):
        epoch = raw_value
    elif isinstance(raw_value, str):
        try:
            epoch = int(raw_value)
        except ValueError:
            return raw_value
    else:
        return str(raw_value)

    if epoch <= 0:
        return ""

    try:
        dt = datetime.fromtimestamp(epoch).astimezone()
    except (OSError, OverflowError, ValueError):
        return str(raw_value)

    display = dt.strftime("%Y-%m-%d %H:%M:%S")
    offset = dt.strftime("%z")
    if offset:
        return "{} (UTC{}:{})".format(display, offset[:3], offset[3:])
    timezone_name = dt.tzname()
    if timezone_name:
        return "{} ({})".format(display, timezone_name)
    return display


@contextmanager
def _oauth_escape_cancel_event():
    cancel_event = threading.Event()
    stop_event = threading.Event()
    listener: threading.Thread | None = None
    fd: int | None = None
    old_terminal_settings = None

    if sys.stdin is None or not sys.stdin.isatty():
        yield cancel_event
        return

    if _IS_WIN32:
        listener = threading.Thread(target=_watch_oauth_escape_win, args=(cancel_event, stop_event), daemon=True)
    else:
        import termios
        import tty

        try:
            fd = sys.stdin.fileno()
            old_terminal_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            yield cancel_event
            return
        listener = threading.Thread(target=_watch_oauth_escape_posix, args=(fd, cancel_event, stop_event), daemon=True)

    listener.start()
    try:
        yield cancel_event
    finally:
        stop_event.set()
        listener.join(timeout=0.2)
        if fd is not None and old_terminal_settings is not None:
            import termios

            termios.tcsetattr(fd, termios.TCSADRAIN, old_terminal_settings)


def _watch_oauth_escape_win(cancel_event: threading.Event, stop_event: threading.Event) -> None:
    try:
        msvcrt = _get_msvcrt()
        while not stop_event.wait(0.05):
            if not msvcrt.kbhit():
                continue
            key = msvcrt.getch()[0]
            if key in (0x00, 0xE0):
                if msvcrt.kbhit():
                    msvcrt.getch()
                continue
            if key in (3, 27):
                cancel_event.set()
                return
    except (Exception, KeyboardInterrupt):
        cancel_event.set()


def _watch_oauth_escape_posix(fd: int, cancel_event: threading.Event, stop_event: threading.Event) -> None:
    import select as select_mod

    while not stop_event.is_set():
        try:
            ready, _, _ = select_mod.select([fd], [], [], 0.05)
        except Exception:
            return
        if not ready:
            continue

        try:
            key = os.read(fd, 1)
        except OSError:
            return

        if key == b"\x03":
            cancel_event.set()
            return
        if key != b"\x1b":
            continue

        # Treat lone Esc as cancel, but consume escape sequences such as arrow keys.
        try:
            ready, _, _ = select_mod.select([fd], [], [], 0.03)
            if ready:
                os.read(fd, 4096)
                continue
        except OSError:
            return

        cancel_event.set()
        return


def _aliyun_oauth_login_flow(existing_cred: "AliyunCredential | None") -> str | None | _BackSentinel:
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
    from iac_code.services.providers.aliyun_oauth import (
        AliyunOAuthCancelledError,
        AliyunOAuthClient,
        AliyunOAuthError,
        get_oauth_site,
        oauth_site_options,
        run_browser_oauth_flow,
    )

    site_options = oauth_site_options()
    site_label_by_type = {
        "CN": _("China"),
        "INTL": _("International"),
    }
    site_idx = _select(_("Choose site type"), [site_label_by_type[site_type] for site_type, _label in site_options])
    if site_idx is None:
        return _BACK

    site_type = site_options[site_idx][0]
    site = get_oauth_site(site_type)
    client = AliyunOAuthClient(site)

    try:
        with _oauth_escape_cancel_event() as cancel_event:
            token = run_browser_oauth_flow(site_type, oauth_client=client, cancel_event=cancel_event)
        sts = client.exchange_access_token_for_sts(token.access_token)
    except AliyunOAuthCancelledError:
        return _BACK
    except AliyunOAuthError as exc:
        return _("Alibaba Cloud OAuth login failed: {error}").format(error=str(exc))

    credential = AliyunCredential(
        mode="OAuth",
        region_id=existing_cred.region_id if existing_cred else "cn-hangzhou",
        oauth_site_type=site_type,
        oauth_access_token=token.access_token,
        oauth_refresh_token=token.refresh_token,
        oauth_access_token_expire=token.access_token_expire,
        oauth_refresh_token_expire=token.refresh_token_expire,
        access_key_id=sts.access_key_id,
        access_key_secret=sts.access_key_secret,
        sts_token=sts.sts_token,
        sts_expiration=sts.sts_expiration,
    )
    AliyunCredentials.save(credential)
    return _("Configured: Alibaba Cloud OAuth credentials saved")


def _aliyun_credential_flow() -> str | None | _BackSentinel:
    """Configure Aliyun credentials with type selection."""
    from iac_code.services.providers.aliyun import (
        CREDENTIAL_MODES,
        MODE_FIELDS,
        AliyunCredential,
        AliyunCredentials,
    )

    title = _("Configure Alibaba Cloud credentials")

    # Load existing credentials from both sources
    iac_code_cred = AliyunCredentials._load_from_iac_code_config()
    cli_cred = AliyunCredentials.load_from_aliyun_cli()

    # Determine which to display
    existing_cred = iac_code_cred or cli_cred
    source = "iac-code" if iac_code_cred else ("aliyun CLI" if cli_cred else "")

    while True:
        # Show current config if exists, then let user choose to reconfigure or go back
        if existing_cred and source:
            action_options = [_("Reconfigure credential"), _("Back")]
            info = lambda: _render_credential_info(existing_cred, source)  # noqa: E731
            action_idx = _select_with_info(title, action_options, info_renderer=info)
            if action_idx is None or action_idx == 1:
                return _BACK
            # action_idx == 0: continue to reconfigure

        # Select credential mode
        mode_options = [_aliyun_credential_mode_label(mode) for mode in CREDENTIAL_MODES]
        default_mode_idx = 0
        if existing_cred and existing_cred.mode in CREDENTIAL_MODES:
            default_mode_idx = CREDENTIAL_MODES.index(existing_cred.mode)

        mode_idx = _select(_("Select credential type"), mode_options, default_index=default_mode_idx)
        if mode_idx is None:
            if existing_cred and source:
                continue  # Go back to showing current config
            return _BACK

        selected_mode = CREDENTIAL_MODES[mode_idx]
        if selected_mode == "OAuth":
            result = _aliyun_oauth_login_flow(existing_cred)
            if result is _BACK:
                continue
            return result

        mode_fields = MODE_FIELDS[selected_mode]

        # Collect field values
        field_values: dict[str, str] = {}
        for field_name, label, sensitive in mode_fields:
            # Pre-fill from existing credential if same mode
            existing_value = None
            if existing_cred and existing_cred.mode == selected_mode:
                existing_value = getattr(existing_cred, field_name, "") or None

            if sensitive:
                value = _input_masked(title, f"{label}: ", existing=existing_value)
            else:
                if existing_value:
                    value = _input_text_with_default(title, label, existing_value)
                else:
                    value = _input_text(title, f"{label}: ")

            if value is _BACK:
                break  # Go back to mode selection
            if value is None:
                return _("Auth cancelled")

            field_values[field_name] = str(value).strip()

        if len(field_values) != len(mode_fields):
            continue  # User pressed back during field input

        # Validate that required fields are not empty
        if not all(field_values.values()):
            continue

        # Build credential and save
        cred = AliyunCredential(
            mode=selected_mode,
            access_key_id=field_values.get("access_key_id", ""),
            access_key_secret=field_values.get("access_key_secret", ""),
            region_id=existing_cred.region_id if existing_cred else "cn-hangzhou",
            sts_token=field_values.get("sts_token", ""),
            ram_role_arn=field_values.get("ram_role_arn", ""),
            ram_session_name=field_values.get("ram_session_name", ""),
        )
        AliyunCredentials.save(cred)
        return _("Configured: Alibaba Cloud credentials saved to ~/.iac-code")


def _aliyun_region_flow() -> str | None | _BackSentinel:
    """Configure Aliyun default region."""
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials

    title = _("Configure Alibaba Cloud region")

    # Load existing credentials
    iac_code_cred = AliyunCredentials._load_from_iac_code_config()
    cli_cred = AliyunCredentials.load_from_aliyun_cli()
    existing_cred = iac_code_cred or cli_cred
    current_region = existing_cred.region_id if existing_cred else "cn-hangzhou"

    region = _input_text_with_default(title, _("Region"), current_region)
    if region is _BACK:
        return _BACK
    if region is None:
        return _("Auth cancelled")

    region_str = str(region).strip()
    if not region_str:
        region_str = current_region

    if existing_cred:
        existing_cred.region_id = region_str
        AliyunCredentials.save(existing_cred)
    else:
        # No existing credential - save just the region with empty AK credential
        cred = AliyunCredential(region_id=region_str)
        AliyunCredentials.save(cred)

    return _("Configured: Alibaba Cloud region saved to ~/.iac-code")


def _input_text_with_default(title: str, label: str, default: str) -> str | None | _BackSentinel:
    """Full-screen text input with a default value shown. Returns str, None (Ctrl+C), or _BACK (Esc)."""
    chars: list[str] = list(default)
    hints = "Enter {}  Esc {}".format(_("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        text = "".join(chars)
        _write(f"  {label}: {text}")
        _write("\033[s")  # save cursor position
        _write(f"\n\n  {_C_DIM}{hints}{_C_RST}")
        _write("\033[u")  # restore cursor
        _flush()

    draw()

    if _IS_WIN32:
        try:
            while True:
                events = _read_input_events_win()
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    draw()
        except (Exception, KeyboardInterrupt):
            # Ctrl+C on Windows arrives as KeyboardInterrupt — treat as cancel.
            return None
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while True:
                events = _read_input_events(fd)
                need_redraw = False
                done = False

                for event in events:
                    if event[0] == "enter":
                        done = True
                        break
                    elif event[0] == "back":
                        return _BACK
                    elif event[0] == "cancel":
                        return None
                    elif event[0] == "backspace":
                        if chars:
                            chars.pop()
                        need_redraw = True
                    elif event[0] == "char":
                        chars.append(event[1])
                        need_redraw = True

                if done:
                    break
                if need_redraw:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    draw()
                    tty.setraw(fd)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return "".join(chars) if chars else None
