"""Authentication command — interactive provider/key/model setup."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlparse

if TYPE_CHECKING:
    from iac_code.services.providers.aliyun import AliyunCredential

from iac_code.config import (
    _LEGACY_KEY_NAME_ALIASES,
    _load_yaml,
    _save_yaml,
    get_active_provider_key,
    get_credentials_path,
    get_provider_config,
    get_settings_path,
)
from iac_code.i18n import _
from iac_code.services.telemetry import log_event
from iac_code.services.telemetry.names import Events


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


# Provider definitions
PROVIDERS: list[LLMProvider] = [
    {
        "name": "DashScope",
        "display_name": "阿里云百炼",
        "key_name": "dashscope",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            "qwen3.6-max-preview",
            "qwen3.6-plus",
            "qwen3.5-plus",
            "qwen3.5-flash",
            "qwq-plus",
            "kimi-k2.6",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "glm-5.1",
        ],
        "default_model": "qwen3.6-plus",
    },
    {
        "name": "DashScope Token Plan",
        "display_name": "阿里云百炼 Token Plan",
        "key_name": "dashscope_token_plan",
        "api_base": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "models": [
            "qwen3.6-plus",
            "deepseek-v3.2",
            "glm-5",
            "MiniMax-M2.5",
        ],
        "default_model": "qwen3.6-plus",
    },
    {
        "name": "OpenAI",
        "display_name": "OpenAI",
        "key_name": "openai",
        "api_base": None,
        "models": [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.2",
        ],
        "default_model": "gpt-5.5",
    },
    {
        "name": "Anthropic",
        "display_name": "Anthropic",
        "key_name": "anthropic",
        "api_base": None,
        "models": [
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6-1m",
            "claude-haiku-4-5-20251001",
        ],
        "default_model": "claude-opus-4-7",
    },
    {
        "name": "DeepSeek",
        "display_name": "DeepSeek",
        "key_name": "deepseek",
        "api_base": "https://api.deepseek.com/v1",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ],
        "default_model": "deepseek-v4-pro",
    },
    {
        "name": "OpenAPI Compatible",
        "display_name": "OpenAPI 兼容",
        "key_name": "openapi_compatible",
        "api_base": None,
        "models": [],
        "default_model": "",
    },
]

# ── ANSI helpers ──────────────────────────────────────────────────────
_C_SEL = "\033[96m"  # bright cyan (selected)
_C_DIM = "\033[38;2;128;128;128m"  # gray (unselected / hints)
_C_RST = "\033[0m"
_C_BOLD = "\033[1m"

_BACK = _BackSentinel()


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


def _select(title: str, options: list[str], default_index: int = 0) -> int | None:
    """Full-screen selector. Returns index or None (Esc/Ctrl+C)."""
    import select as select_mod
    import termios
    import tty

    selected = default_index
    total = len(options)
    if total == 0:
        return None
    selected = max(0, min(selected, total - 1))

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    hints = "↑↓ {}  Enter {}  Esc {}".format(_("Navigate"), _("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        _render_options(options, selected, hints)

    draw()

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
    import termios
    import tty

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
    import termios
    import tty

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
        context.repl._reinitialize_provider(context.repl.store.get_state().model)

    return result


def _auth_flow(console, store) -> str | None:
    """Auth flow running inside alternate screen."""
    while True:
        # Step 0: Select category
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
            continue  # Go back to category selection
        return result


def _get_active_key_name() -> str:
    """Get the key_name of the currently active provider."""
    return get_active_provider_key() or ""


def _llm_auth_flow(console, store) -> str | None | _BackSentinel:
    """LLM provider auth flow."""
    active_key_name = _get_active_key_name()

    while True:
        # Step 1: Select provider
        provider_options = []
        for p in PROVIDERS:
            label = str(p["display_name"])
            if str(p["key_name"]) == active_key_name:
                label += _(" (current)")
            provider_options.append(label)

        default_idx = 0
        for i, p in enumerate(PROVIDERS):
            if str(p["key_name"]) == active_key_name:
                default_idx = i
                break

        idx = _select(_("Select provider"), provider_options, default_index=default_idx)
        if idx is None:
            return _BACK

        provider = PROVIDERS[idx]

        # Step 2 (OpenAPI Compatible only): API Base URL
        user_api_base = None
        if provider["key_name"] == "openapi_compatible":
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

        # Step 3: API key
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

        # Step 4: Select model — recall this provider's own last-used model,
        # so switching providers never leaks another provider's custom model.
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

        # Log telemetry event
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
    import select as select_mod
    import termios
    import tty

    selected = default_index
    total = len(options)
    if total == 0:
        return None
    selected = max(0, min(selected, total - 1))

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    hints = "↑↓ {}  Enter {}  Esc {}".format(_("Navigate"), _("Confirm"), _("Back"))

    def draw():
        _clear_screen()
        _render_title(title)
        if callable(info_renderer):
            info_renderer()
        _render_options(options, selected, hints)

    draw()

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
    from iac_code.services.providers.aliyun import MODE_DISPLAY_NAMES, MODE_FIELDS, mask_sensitive

    _write("  {}{} ({}){}\n".format(_C_DIM, _("Current configuration"), source, _C_RST))
    mode_display = _(MODE_DISPLAY_NAMES.get(credential.mode, credential.mode))
    _write("  {}{}: {}{}\n".format(_C_DIM, _("Mode"), mode_display, _C_RST))

    mode_fields = MODE_FIELDS.get(credential.mode, [])
    for field_name, label, sensitive in mode_fields:
        value = getattr(credential, field_name, "")
        if value and sensitive:
            value = mask_sensitive(value)
        display_value = value if value else _("(not set)")
        _write(f"  {_C_DIM}{label}: {display_value}{_C_RST}\n")

    _write("  {}{}: {}{}\n".format(_C_DIM, _("Region"), credential.region_id, _C_RST))
    _write("\n")


def _aliyun_credential_flow() -> str | None | _BackSentinel:
    """Configure Aliyun credentials with type selection."""
    from iac_code.services.providers.aliyun import (
        CREDENTIAL_MODES,
        MODE_DISPLAY_NAMES,
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
        mode_options = [_(MODE_DISPLAY_NAMES[m]) for m in CREDENTIAL_MODES]
        default_mode_idx = 0
        if existing_cred and existing_cred.mode in CREDENTIAL_MODES:
            default_mode_idx = CREDENTIAL_MODES.index(existing_cred.mode)

        mode_idx = _select(_("Select credential type"), mode_options, default_index=default_mode_idx)
        if mode_idx is None:
            if existing_cred and source:
                continue  # Go back to showing current config
            return _BACK

        selected_mode = CREDENTIAL_MODES[mode_idx]
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
    import termios
    import tty

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
