"""Settings API helpers for the local Web workbench."""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from iac_code.config import (
    _LEGACY_KEY_NAME_ALIASES,
    _load_yaml,
    _save_yaml,
    get_active_provider_key,
    get_available_partner_sources,
    get_credentials_path,
    get_llm_source,
    get_provider_config,
    get_settings_path,
    load_credentials,
)
from iac_code.i18n import LANGUAGE_DISPLAY_NAMES, SUPPORTED_LANGUAGES, _, set_language
from iac_code.pipeline.config import is_selling_review_step_enabled, save_selling_review_step_enabled
from iac_code.providers.registry import PROVIDER_GROUPS, PROVIDER_REGISTRY, ProviderDescriptor
from iac_code.providers.thinking import get_thinking_spec, normalize_effort, resolve_thinking_active
from iac_code.services.providers.aliyun import CREDENTIAL_MODES, DEFAULT_REGION, AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_oauth import (
    AliyunOAuthError,
    OAuthAuthorization,
    create_oauth_authorization,
    exchange_oauth_authorization_code,
    fixed_manual_redirect_uri,
    run_browser_oauth_flow,
)
from iac_code.types.permissions import PermissionMode

_FOREIGN_SESSIONS_SETTINGS_KEY = "foreignSessions"

# 区分「请求里没带该字段」(保持现状)与「带了但为空/null」(清除回落默认)。
_UNSET = object()

# 「最大输出 tokens」留空时的回落默认:与各 provider.stream(max_tokens=8192) 的请求层默认一致
# (registry 无按模型上限,全局统一)。仅用于前端 placeholder 提示,让「留空使用模型默认」显示具体数值。
_DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 8192


def is_foreign_pipeline_visible() -> bool:
    """Return whether foreign pipeline sessions are shown (default ``False``)."""
    settings = _load_yaml(get_settings_path())
    section = settings.get(_FOREIGN_SESSIONS_SETTINGS_KEY)
    if not isinstance(section, dict):
        return False
    return bool(section.get("showPipeline", False))


def is_foreign_normal_visible() -> bool:
    """Return whether foreign normal sessions are shown (default ``False``)."""
    settings = _load_yaml(get_settings_path())
    section = settings.get(_FOREIGN_SESSIONS_SETTINGS_KEY)
    if not isinstance(section, dict):
        return False
    return bool(section.get("showNormal", False))


def save_foreign_sessions_visibility(show_pipeline: bool, show_normal: bool) -> dict[str, bool]:
    """Persist foreign-session visibility flags, preserving other section keys."""
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_FOREIGN_SESSIONS_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section["showPipeline"] = bool(show_pipeline)
    section["showNormal"] = bool(show_normal)
    settings[_FOREIGN_SESSIONS_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    return {"showPipeline": bool(show_pipeline), "showNormal": bool(show_normal)}


def selling_review_step_settings() -> dict[str, bool]:
    """Return the persisted selling-pipeline review-step toggle for the settings API."""
    return {"enabled": is_selling_review_step_enabled()}


def save_selling_review_step(enabled: bool) -> dict[str, bool]:
    """Persist the selling-pipeline review-step toggle and echo it back."""
    return {"enabled": save_selling_review_step_enabled(enabled)}


_DEVELOPER_SETTINGS_KEY = "developer"


def developer_settings() -> dict[str, bool]:
    """Return the developer-mode flags for the settings API (both default ``False``).

    ``mode`` gates the Developer settings tab; ``highlightFailedTools`` controls
    whether failed tool calls are painted red (off = rendered like any other tool).
    """
    settings = _load_yaml(get_settings_path())
    section = settings.get(_DEVELOPER_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    return {
        "mode": bool(section.get("mode", False)),
        "highlightFailedTools": bool(section.get("highlightFailedTools", False)),
    }


def save_developer_settings(mode: bool, highlight_failed_tools: bool) -> dict[str, bool]:
    """Persist the developer-mode flags, preserving other section keys."""
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_DEVELOPER_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section["mode"] = bool(mode)
    section["highlightFailedTools"] = bool(highlight_failed_tools)
    settings[_DEVELOPER_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    return {"mode": bool(mode), "highlightFailedTools": bool(highlight_failed_tools)}


_APPEARANCE_SETTINGS_KEY = "appearance"
VALID_THEMES = ("graphite", "midnight", "evergreen", "sepia", "ivory")
DEFAULT_THEME = "graphite"


def get_appearance_theme() -> str:
    """Return the persisted UI theme slug, falling back to the default."""
    settings = _load_yaml(get_settings_path())
    section = settings.get(_APPEARANCE_SETTINGS_KEY)
    if not isinstance(section, dict):
        return DEFAULT_THEME
    theme = section.get("theme")
    return theme if theme in VALID_THEMES else DEFAULT_THEME


def save_appearance_theme(theme: str) -> dict[str, str]:
    """Persist the UI theme slug, preserving other section keys."""
    if theme not in VALID_THEMES:
        raise ValueError(_("unknown theme"))
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_APPEARANCE_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section["theme"] = theme
    settings[_APPEARANCE_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    return {"theme": theme}


_UI_SETTINGS_KEY = "ui"


def get_ui_language() -> str | None:
    """Return the persisted UI language code, or ``None`` when unset/unknown."""
    settings = _load_yaml(get_settings_path())
    section = settings.get(_UI_SETTINGS_KEY)
    if not isinstance(section, dict):
        return None
    lang = section.get("language")
    return lang if lang in SUPPORTED_LANGUAGES else None


def save_ui_language(lang: str) -> dict[str, str]:
    """Persist the UI language code, preserving other section keys."""
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(_("unknown language"))
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_UI_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section["language"] = lang
    settings[_UI_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    set_language(lang)
    return {"language": lang}


def ui_language_payload() -> dict[str, Any]:
    """Return the saved UI language plus the available-languages list."""
    return {
        "uiLanguage": get_ui_language(),
        "availableLanguages": [{"code": code, "name": LANGUAGE_DISPLAY_NAMES[code]} for code in SUPPORTED_LANGUAGES],
    }


_SESSION_DEFAULTS_SETTINGS_KEY = "sessionDefaults"
_VALID_SESSION_MODES = ("normal", "pipeline")
_VALID_PERMISSION_MODES = frozenset(mode.value for mode in PermissionMode)
DEFAULT_SESSION_PERMISSION_MODE = PermissionMode.DEFAULT.value
DEFAULT_SESSION_MODE = "normal"
# 前端 PIPELINE_OPTIONS 目前只有 selling(售卖流水线);流水线默认落此 flavor。
DEFAULT_SESSION_PIPELINE_NAME = "selling"


def get_session_defaults() -> dict[str, str]:
    """Return the persisted new-session defaults, falling back per field.

    Controls the initial permission mode and session mode a *new* session draft
    starts with (existing sessions keep their own stored choices). Unknown or
    missing stored values fall back to the safe defaults.
    """
    settings = _load_yaml(get_settings_path())
    section = settings.get(_SESSION_DEFAULTS_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    permission_mode = section.get("permissionMode")
    if permission_mode not in _VALID_PERMISSION_MODES:
        permission_mode = DEFAULT_SESSION_PERMISSION_MODE
    mode = section.get("mode")
    if mode not in _VALID_SESSION_MODES:
        mode = DEFAULT_SESSION_MODE
    pipeline_name = section.get("pipelineName")
    if not isinstance(pipeline_name, str) or not pipeline_name.strip():
        pipeline_name = DEFAULT_SESSION_PIPELINE_NAME
    return {"permissionMode": permission_mode, "mode": mode, "pipelineName": pipeline_name.strip()}


def save_session_defaults(
    permission_mode: str,
    mode: str,
    pipeline_name: str | None = None,
) -> dict[str, str]:
    """Persist new-session defaults, preserving other section keys.

    ``pipeline_name`` is always stored (even when ``mode`` is ``normal``) so a
    previously chosen pipeline flavor survives toggling the mode back and forth;
    it only takes effect when ``mode`` is ``pipeline``.
    """
    if permission_mode not in _VALID_PERMISSION_MODES:
        raise ValueError(_("unknown permission mode"))
    if mode not in _VALID_SESSION_MODES:
        raise ValueError(_("mode must be normal or pipeline"))
    resolved_pipeline = (
        pipeline_name.strip()
        if isinstance(pipeline_name, str) and pipeline_name.strip()
        else DEFAULT_SESSION_PIPELINE_NAME
    )
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_SESSION_DEFAULTS_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section["permissionMode"] = permission_mode
    section["mode"] = mode
    section["pipelineName"] = resolved_pipeline
    settings[_SESSION_DEFAULTS_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    return {"permissionMode": permission_mode, "mode": mode, "pipelineName": resolved_pipeline}


def providers_payload() -> dict[str, Any]:
    """Return provider/model capabilities plus the active provider summary.

    Providers are ordered and labelled by the shared ``PROVIDER_GROUPS`` so the
    Web workbench mirrors the REPL ``/auth`` flow. Locally-available partner
    sources (e.g. QwenPaw) are surfaced first as read-only entries.
    """
    active = active_provider_summary()
    credentials = load_credentials(model=_string_or_none(active.get("model")))
    active_provider = _string_or_none(active.get("provider"))
    providers: list[dict[str, Any]] = list(_partner_payloads())
    for group_label, provider in _grouped_providers():
        providers.append(
            _provider_payload(
                provider,
                group=group_label,
                credentials=credentials,
                active_provider=active_provider,
            )
        )
    return {
        "providers": providers,
        "active": active,
    }


def _grouped_providers() -> list[tuple[str, ProviderDescriptor]]:
    """Yield (translated group label, descriptor) in shared group order.

    Any registry provider not covered by ``PROVIDER_GROUPS`` is appended under a
    generic "Other" group so nothing silently disappears from the payload.
    """
    ordered: list[tuple[str, ProviderDescriptor]] = []
    seen: set[str] = set()
    for group_name, keys in PROVIDER_GROUPS:
        label = _(group_name)
        for key in keys:
            provider = PROVIDER_REGISTRY.get(key)
            if provider is None:
                continue
            ordered.append((label, provider))
            seen.add(key)
    leftover = [p for key, p in PROVIDER_REGISTRY.items() if key not in seen]
    if leftover:
        other = _("Other")
        ordered.extend((other, provider) for provider in leftover)
    return ordered


def _partner_payloads() -> list[dict[str, Any]]:
    """Build read-only nav entries for locally-available partner sources."""
    partners = get_available_partner_sources()
    if not partners:
        return []
    current_source = get_llm_source()
    group_label = _("Third-party")
    note = _("Managed by external login")
    entries: list[dict[str, Any]] = []
    for partner in partners:
        provider_display = partner.get_provider_display() or None
        entries.append(
            {
                "key": "partner:{}".format(partner.key),
                "name": _(partner.display_name),
                "displayName": _(partner.display_name),
                "group": group_label,
                "kind": "partner",
                "readOnly": True,
                "note": note,
                "providerLabel": provider_display,
                "current": partner.key == current_source,
                "usable": True,
                "configured": partner.key == current_source,
                # 只读伙伴条目无可编辑表单字段,给前端一致的默认值。
                "requireApiKey": False,
                "isLocal": False,
                "hasApiKey": True,
                "efforts": [],
                "models": [],
                "apiBase": None,
                "defaultModel": None,
                "savedModel": None,
                "savedApiBase": None,
                "savedEffort": None,
                "savedThinkingBudget": None,
                "savedMaxCompletionTokens": None,
                "savedApiKey": None,
            }
        )
    return entries


def save_active_provider(data: dict[str, Any]) -> dict[str, Any]:
    """Persist active LLM provider settings and return the redacted summary."""
    provider_key = _required_string(data, "provider")
    model = _required_string(data, "model")
    effort = _optional_string(data, "effort")
    api_base = _optional_string(data, "apiBase")
    api_key = _optional_string(data, "apiKey")
    thinking_budget = _optional_int_field(data, "thinkingBudget")
    max_completion_tokens = _optional_int_field(data, "maxCompletionTokens")

    provider = PROVIDER_REGISTRY.get(provider_key)
    if provider is None:
        raise ValueError(_("unknown provider"))
    _validate_model(provider, model)
    if effort is not None:
        _validate_effort(provider_key, model, effort)
    _validate_positive_int(thinking_budget, "thinkingBudget")
    _validate_positive_int(max_completion_tokens, "maxCompletionTokens")

    _save_active_provider_config(
        provider,
        model,
        effort=effort,
        api_base=api_base,
        thinking_budget=thinking_budget,
        max_completion_tokens=max_completion_tokens,
    )
    if api_key is not None:
        _save_llm_key(provider_key, api_key)
    return {"active": active_provider_summary()}


def save_provider_config(data: dict[str, Any]) -> dict[str, Any]:
    """Persist a provider's config without changing the active provider."""
    provider_key = _required_string(data, "provider")
    model = _required_string(data, "model")
    effort = _optional_string(data, "effort")
    api_base = _optional_string(data, "apiBase")
    api_key = _optional_string(data, "apiKey")
    thinking_budget = _optional_int_field(data, "thinkingBudget")
    max_completion_tokens = _optional_int_field(data, "maxCompletionTokens")

    provider = PROVIDER_REGISTRY.get(provider_key)
    if provider is None:
        raise ValueError(_("unknown provider"))
    _validate_model(provider, model)
    if effort is not None:
        _validate_effort(provider_key, model, effort)
    _validate_positive_int(thinking_budget, "thinkingBudget")
    _validate_positive_int(max_completion_tokens, "maxCompletionTokens")

    _write_provider_config(
        provider,
        model,
        effort=effort,
        api_base=api_base,
        activate=False,
        thinking_budget=thinking_budget,
        max_completion_tokens=max_completion_tokens,
    )
    if api_key is not None:
        _save_llm_key(provider_key, api_key)
    return providers_payload()


def set_active_provider(data: dict[str, Any]) -> dict[str, Any]:
    """Mark an already-configured provider (or a partner source) as active."""
    provider_key = _required_string(data, "provider")
    if provider_key.startswith("partner:"):
        return _set_active_partner(provider_key)
    provider = PROVIDER_REGISTRY.get(provider_key)
    if provider is None:
        raise ValueError(_("unknown provider"))
    saved = get_provider_config(provider_key)
    if not _string_or_none(saved.get("model")):
        raise ValueError(_("provider is not configured"))

    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    config["activeProvider"] = provider_key
    _save_yaml(settings_path, config)
    return {"active": active_provider_summary()}


def clear_provider_config(data: dict[str, Any]) -> dict[str, Any]:
    """Remove a provider's saved config and API key, back to unconfigured.

    Deletes the ``providers.<key>`` entry from ``settings.yml`` and the provider
    key from ``.credentials.yml`` (both including legacy aliases). Refuses when
    the provider is the active one — the user must switch the active model first.
    """
    provider_key = _required_string(data, "provider")
    provider = PROVIDER_REGISTRY.get(provider_key)
    if provider is None:
        raise ValueError(_("unknown provider"))
    if provider_key == get_active_provider_key():
        raise ValueError(_("cannot clear active provider"))

    _remove_provider_config(provider_key)
    _remove_llm_key(provider_key)
    return providers_payload()


def _set_active_partner(provider_key: str) -> dict[str, Any]:
    """Activate a read-only partner source (e.g. ``partner:qwenpaw``).

    Mirrors the REPL ``/auth`` third-party flow: drop ``activeProvider`` so the
    partner ``llm_source`` takes effect (see ``get_llm_source`` priority chain).
    """
    partner_key = provider_key.split(":", 1)[1]
    if not any(source.key == partner_key for source in get_available_partner_sources()):
        raise ValueError(_("unknown partner source"))

    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    config.pop("activeProvider", None)
    config["llm_source"] = partner_key
    _save_yaml(settings_path, config)
    return {"active": active_provider_summary()}


def active_provider_summary() -> dict[str, Any]:
    """Return active provider settings without credential material."""
    provider_key = get_active_provider_key()
    if not provider_key:
        return {
            "provider": None,
            "model": None,
            "effort": None,
            "apiBase": None,
            "hasApiKey": False,
        }

    config = get_provider_config(provider_key)
    model = _string_or_none(config.get("model"))
    effort = _string_or_none(config.get("effort"))
    api_base = _string_or_none(config.get("apiBase"))
    credentials = load_credentials(model=model)
    return {
        "provider": provider_key,
        "model": model,
        "effort": effort,
        "apiBase": api_base,
        "hasApiKey": bool(credentials.get(provider_key)),
    }


def aliyun_cloud_summary() -> dict[str, Any]:
    """Return the Alibaba Cloud credential summary without secrets."""
    credential = _load_existing_aliyun_credential(strict=True)
    if credential is None:
        summary: dict[str, Any] = {
            "configured": False,
            "mode": None,
            "region": None,
            "expiration": None,
            "oauthSiteType": None,
            "oauthAccessTokenExpire": None,
            "oauthRefreshTokenExpire": None,
            "stsExpiration": None,
            "accessKeyId": None,
            "accessKeySecret": None,
            "stsToken": None,
            "ramRoleArn": None,
            "ramSessionName": None,
        }
    else:
        summary = _aliyun_summary(credential)
    detected_credential, source = _detect_aliyun_effective()
    summary["detected"] = _detected_summary(detected_credential, source)
    return summary


def _mask_head(value: str | None, keep: int = 4) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= keep:
        return text + "*" * 4
    return text[:keep] + "*" * min(len(text) - keep, 4)


def _detect_aliyun_effective() -> tuple[AliyunCredential | None, str | None]:
    env_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
    env_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
    if env_id and env_secret:
        return (
            AliyunCredential(
                mode="AK",
                access_key_id=env_id,
                access_key_secret=env_secret,
                sts_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", "").strip(),
                region_id=os.environ.get("ALIBABA_CLOUD_REGION_ID", "").strip() or DEFAULT_REGION,
            ),
            "env",
        )
    from_config = AliyunCredentials._load_from_iac_code_config()
    if from_config is not None:
        return from_config, "config"
    from_cli = AliyunCredentials._load_from_aliyun_cli()
    if from_cli is not None:
        return from_cli, "cli"
    return None, None


def _detected_summary(credential: AliyunCredential | None, source: str | None) -> dict[str, Any] | None:
    if credential is None or source is None:
        return None
    return {
        "source": source,
        "mode": credential.mode,
        "region": credential.region_id or "",
        "accessKeyId": _mask_head(credential.access_key_id),
        "hasAccessKeySecret": bool((credential.access_key_secret or "").strip()),
        "hasStsToken": bool((credential.sts_token or "").strip()),
        "ramRoleArn": credential.ram_role_arn or "",
        "ramSessionName": credential.ram_session_name or "",
        "oauthSiteType": credential.oauth_site_type or "",
    }


def save_aliyun_cloud(data: dict[str, Any]) -> dict[str, Any]:
    """Persist Alibaba Cloud credentials and return the redacted summary."""
    mode = _required_string(data, "mode")
    if mode not in CREDENTIAL_MODES:
        raise ValueError(_("unknown aliyun credential mode"))

    existing = AliyunCredentials.load()
    credential = AliyunCredential(
        mode=mode,
        access_key_id=_merged_string(data, existing, "access_key_id", "accessKeyId", "access_key_id"),
        access_key_secret=_merged_string(data, existing, "access_key_secret", "accessKeySecret", "access_key_secret"),
        region_id=_merged_string(data, existing, "region_id", "region", "regionId", "region_id", default=DEFAULT_REGION)
        or DEFAULT_REGION,
        sts_token=_merged_string(data, existing, "sts_token", "stsToken", "sts_token"),
        sts_expiration=_merged_int(data, existing, "sts_expiration", "stsExpiration", "sts_expiration", "expiration"),
        ram_role_arn=_merged_string(data, existing, "ram_role_arn", "ramRoleArn", "ram_role_arn"),
        ram_session_name=_merged_string(data, existing, "ram_session_name", "ramSessionName", "ram_session_name"),
        oauth_site_type=_merged_string(data, existing, "oauth_site_type", "oauthSiteType", "oauth_site_type"),
        oauth_access_token=_merged_string(
            data,
            existing,
            "oauth_access_token",
            "oauthAccessToken",
            "oauth_access_token",
        ),
        oauth_refresh_token=_merged_string(
            data,
            existing,
            "oauth_refresh_token",
            "oauthRefreshToken",
            "oauth_refresh_token",
        ),
        oauth_access_token_expire=_merged_int(
            data,
            existing,
            "oauth_access_token_expire",
            "oauthAccessTokenExpire",
            "oauth_access_token_expire",
        ),
        oauth_refresh_token_expire=_merged_int(
            data,
            existing,
            "oauth_refresh_token_expire",
            "oauthRefreshTokenExpire",
            "oauth_refresh_token_expire",
        ),
    )
    _prune_aliyun_credential_for_mode(credential)
    _validate_aliyun_credential(credential)
    AliyunCredentials.save(credential)
    return _aliyun_summary(credential)


def _save_aliyun_oauth_token(site: str, region: str, token: Any) -> dict[str, Any]:
    existing = AliyunCredentials.load()
    existing_region = existing.region_id if existing is not None else ""
    credential = AliyunCredential(
        mode="OAuth",
        oauth_site_type=site,
        oauth_access_token=token.access_token,
        oauth_refresh_token=token.refresh_token,
        oauth_access_token_expire=token.access_token_expire,
        oauth_refresh_token_expire=token.refresh_token_expire,
        region_id=region or existing_region or DEFAULT_REGION,
    )
    try:
        credential = AliyunCredentials.refresh_oauth_if_needed(credential)
    except AliyunOAuthError:
        pass
    AliyunCredentials.save(credential)
    return _aliyun_summary(credential)


def login_aliyun_oauth(data: dict[str, Any]) -> dict[str, Any]:
    """Run the existing loopback browser flow and persist its credential."""
    site = _required_string(data, "site", "siteType", "oauthSiteType")
    region = _optional_string(data, "region", "regionId") or ""
    try:
        token = run_browser_oauth_flow(site)
    except AliyunOAuthError as exc:
        raise ValueError(str(exc)) from exc
    return _save_aliyun_oauth_token(site, region, token)


@dataclass(frozen=True)
class _ManualAliyunOAuthFlow:
    authorization: OAuthAuthorization
    region: str
    expires_at: float


class AliyunOAuthManualFlowStore:
    """Short-lived in-memory PKCE flows for token-mode manual completion."""

    def __init__(self, *, ttl_seconds: int = 300, clock: Any = time.time) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._flows: dict[str, _ManualAliyunOAuthFlow] = {}
        self._lock = threading.Lock()

    def start(self, data: dict[str, Any]) -> dict[str, Any]:
        site = _required_string(data, "site", "siteType", "oauthSiteType")
        region = _optional_string(data, "region", "regionId") or ""
        authorization = create_oauth_authorization(site, fixed_manual_redirect_uri())
        flow_id = secrets.token_urlsafe(24)
        expires_at = float(self._clock()) + self._ttl_seconds
        with self._lock:
            self._remove_expired(float(self._clock()))
            self._flows[flow_id] = _ManualAliyunOAuthFlow(authorization, region, expires_at)
        return {
            "flowId": flow_id,
            "authorizationUrl": authorization.authorization_url,
            "expiresAt": int(expires_at),
        }

    def complete(self, data: dict[str, Any]) -> dict[str, Any]:
        flow_id = _required_string(data, "flowId")
        submitted = _required_string(data, "callback", "callbackUrl", "authorizationCode", "code").strip()
        now = float(self._clock())
        with self._lock:
            self._remove_expired(now)
            flow = self._flows.pop(flow_id, None)
        if flow is None or flow.expires_at <= now:
            raise ValueError(_("OAuth authorization flow is invalid or expired."))
        code = self._authorization_code(flow.authorization, submitted)
        try:
            token = exchange_oauth_authorization_code(flow.authorization, code)
        except AliyunOAuthError as exc:
            raise ValueError(str(exc)) from exc
        return _save_aliyun_oauth_token(flow.authorization.site_type, flow.region, token)

    def _remove_expired(self, now: float) -> None:
        for flow_id in [key for key, value in self._flows.items() if value.expires_at <= now]:
            self._flows.pop(flow_id, None)

    @staticmethod
    def _authorization_code(authorization: OAuthAuthorization, submitted: str) -> str:
        if not submitted:
            raise ValueError(_("Authorization code is required."))
        if "://" not in submitted:
            return submitted
        parsed = urlparse(submitted)
        expected = urlparse(authorization.redirect_uri)
        if (
            parsed.scheme != expected.scheme
            or parsed.hostname != expected.hostname
            or parsed.port != expected.port
            or parsed.path != expected.path
        ):
            raise ValueError(_("OAuth callback URL does not match the authorization flow."))
        query = parse_qs(parsed.query, keep_blank_values=True)
        states = query.get("state", [])
        codes = query.get("code", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], authorization.state):
            raise ValueError(_("OAuth callback state is invalid."))
        if len(codes) != 1 or not codes[0]:
            raise ValueError(_("Authorization code is required."))
        return codes[0]


def _provider_payload(
    provider: ProviderDescriptor,
    *,
    group: str,
    credentials: dict[str, str],
    active_provider: str | None,
) -> dict[str, Any]:
    provider_config = get_provider_config(provider.key)
    models = [_model_payload(provider.key, model, provider_config) for model in provider.models]
    efforts = sorted({effort for model in models for effort in model["efforts"]})
    has_api_key = bool(credentials.get(provider.key))
    has_saved_config = bool(provider_config)
    # 凭证条件:无需密钥(本地模型或不要求密钥)或已提供密钥。
    credential_ok = has_api_key or provider.is_local or not provider.require_api_key
    # 「可用」(点亮绿点)还须有可用模型:已保存模型,或注册表默认模型。仅有密钥/本地
    # 但没有模型(如兼容模式、本地模型尚未填模型)不算可用,避免误点亮绿点。
    effective_model = _string_or_none(provider_config.get("model")) or _string_or_none(provider.default_model)
    # 有效 base_url:已保存的 apiBase,或注册表默认。兼容模式(注册表无默认端点且无内置模型,
    # 如 openai_compatible / anthropic_compatible)必须用户自填 base_url 才能连;本地模型与其余
    # provider 已有默认端点或 SDK 默认,不作强制。
    effective_api_base = _string_or_none(provider_config.get("apiBase")) or _string_or_none(provider.base_url)
    needs_api_base = provider.base_url is None and not provider.models
    api_base_ok = not needs_api_base or bool(effective_api_base)
    usable = credential_ok and bool(effective_model) and api_base_ok
    configured = provider.key == active_provider or (has_saved_config and usable)
    return {
        "key": provider.key,
        "name": _(provider.display_name),
        "displayName": _(provider.display_name),
        "group": group,
        "kind": "provider",
        "apiBase": provider.base_url,
        "defaultModel": provider.default_model or None,
        "requireApiKey": provider.require_api_key,
        "isLocal": provider.is_local,
        "hasApiKey": has_api_key,
        "usable": usable,
        "configured": configured,
        "efforts": efforts,
        "models": models,
        "savedModel": _string_or_none(provider_config.get("model")),
        "savedApiBase": _string_or_none(provider_config.get("apiBase")),
        "savedEffort": _string_or_none(provider_config.get("effort")),
        "savedThinkingBudget": _int_or_none(provider_config.get("thinkingBudget")),
        "savedMaxCompletionTokens": _int_or_none(provider_config.get("maxCompletionTokens")),
        # 本地单用户工作台:回填已保存密钥,便于在页面上以密文形式查看/编辑。
        "savedApiKey": _string_or_none(credentials.get(provider.key)),
    }


def _model_payload(provider_key: str, model, provider_config: dict[str, Any] | None = None) -> dict[str, Any]:
    thinking = get_thinking_spec(provider_key, model.id)
    cfg = provider_config if isinstance(provider_config, dict) else {}
    models_cfg = cfg.get("models")
    model_cfg = models_cfg.get(model.id) if isinstance(models_cfg, dict) else None
    model_cfg = model_cfg if isinstance(model_cfg, dict) else {}

    def _saved(key: str) -> int | None:
        # 预填按模型级优先、回落 provider 顶层(与 manager 读路径一致),使 UI 显示 == 实际生效值。
        value = _int_or_none(model_cfg.get(key))
        return value if value is not None else _int_or_none(cfg.get(key))

    return {
        "id": model.id,
        "default": model.is_default,
        "supportsMultimodal": model.support_multimodal,
        "efforts": [effort.value for effort in thinking.allowed_efforts],
        "defaultEffort": thinking.default_effort.value if thinking.default_effort is not None else None,
        # 无会话级覆盖时该模型是否默认思考(家族相关):新会话草稿据此点亮「思考」按钮。
        "thinkingDefault": resolve_thinking_active(provider_key, model.id, None),
        # 「思考预算」字段仅对支持独立预算的模型可见(能力门控);其余家族走 effort 推导。
        "supportsThinkingBudget": thinking.supports_thinking_budget,
        "defaultThinkingBudget": thinking.default_thinking_budget,
        # 「最大输出 tokens」对所有模型生效,留空回落到该默认(前端 placeholder 展示)。
        "defaultMaxCompletionTokens": _DEFAULT_MODEL_MAX_OUTPUT_TOKENS,
        # 已保存的两个旋钮按模型回填(providers.<key>.models.<id>,回落 provider 顶层)。
        "savedThinkingBudget": _saved("thinkingBudget"),
        "savedMaxCompletionTokens": _saved("maxCompletionTokens"),
    }


def _validate_model(provider: ProviderDescriptor, model: str) -> None:
    if provider.model_ids and model not in provider.model_ids:
        raise ValueError(_("unknown model"))


def _validate_effort(provider_key: str, model: str, effort: str) -> None:
    normalized = normalize_effort(effort)
    if normalized is None:
        raise ValueError(_("unknown effort"))
    thinking = get_thinking_spec(provider_key, model)
    allowed = {item.value for item in thinking.allowed_efforts}
    # 无已知推理强度规格(手动输入模型/兼容模式等)时放行任意合法强度,
    # 支持前端组合框的自由输入;有规格时仍强制校验。
    if allowed and normalized not in allowed:
        raise ValueError(_("unknown effort"))


def _write_provider_config(
    provider: ProviderDescriptor,
    model: str,
    *,
    effort: str | None,
    api_base: str | None,
    activate: bool,
    thinking_budget: Any = _UNSET,
    max_completion_tokens: Any = _UNSET,
) -> None:
    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}

    existing = providers.get(provider.key)
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry["name"] = provider.name
    entry["model"] = model
    effective_api_base = api_base if api_base is not None else provider.base_url
    if effective_api_base is not None:
        entry["apiBase"] = effective_api_base
    if effort is not None:
        entry["effort"] = effort
    # 「最大输出 tokens」/「思考预算」按模型存于 providers.<key>.models.<model> 下
    # (读路径 manager._get_positive_int_provider_config_value 亦先查模型级、再回落 provider 顶层),
    # 使同一 provider 下不同模型互不干扰。effort 仍存 provider 顶层,不受影响。
    _apply_model_int_knobs(
        entry,
        model,
        thinking_budget=thinking_budget,
        max_completion_tokens=max_completion_tokens,
    )

    providers[provider.key] = entry
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == provider.key:
            providers.pop(legacy, None)
    config["providers"] = providers
    if activate:
        config["activeProvider"] = provider.key
    _save_yaml(settings_path, config)


def _save_active_provider_config(
    provider: ProviderDescriptor,
    model: str,
    *,
    effort: str | None,
    api_base: str | None,
    thinking_budget: Any = _UNSET,
    max_completion_tokens: Any = _UNSET,
) -> None:
    _write_provider_config(
        provider,
        model,
        effort=effort,
        api_base=api_base,
        activate=True,
        thinking_budget=thinking_budget,
        max_completion_tokens=max_completion_tokens,
    )


def _save_llm_key(provider_key: str, api_key: str) -> None:
    keys_path = get_credentials_path()
    keys = _load_yaml(keys_path)
    keys[provider_key] = api_key
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == provider_key:
            keys.pop(legacy, None)
    _save_yaml(keys_path, keys)


def _remove_provider_config(provider_key: str) -> None:
    settings_path = get_settings_path()
    config = _load_yaml(settings_path)
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return
    removed = providers.pop(provider_key, None) is not None
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == provider_key and providers.pop(legacy, None) is not None:
            removed = True
    if removed:
        config["providers"] = providers
        _save_yaml(settings_path, config)


def _remove_llm_key(provider_key: str) -> None:
    keys_path = get_credentials_path()
    keys = _load_yaml(keys_path)
    changed = keys.pop(provider_key, None) is not None
    for legacy, canonical in _LEGACY_KEY_NAME_ALIASES.items():
        if canonical == provider_key and keys.pop(legacy, None) is not None:
            changed = True
    if changed:
        _save_yaml(keys_path, keys)


def _aliyun_summary(credential: AliyunCredential) -> dict[str, Any]:
    return {
        "configured": _is_aliyun_credential_configured(credential),
        "mode": credential.mode,
        "region": credential.region_id or None,
        "expiration": _aliyun_expiration(credential),
        # OAuth 登录站点(CN/INTL),供前端回填「登录站点」选择框;未设置回报 None。
        "oauthSiteType": credential.oauth_site_type or None,
        # OAuth 令牌过期时间(秒级 Unix 时间戳),供前端按本地时区展示;0/未设置回报 None。
        "oauthAccessTokenExpire": credential.oauth_access_token_expire or None,
        "oauthRefreshTokenExpire": credential.oauth_refresh_token_expire or None,
        # OAuth 登录会派生出 STS 临时凭证(见 refresh_oauth_if_needed),其到期时间与
        # 访问/刷新令牌的到期时间彼此独立;单独回传供前端在 OAuth 面板展示第三行。0/未设置回报 None。
        "stsExpiration": credential.sts_expiration or None,
        # 回传已保存的原始凭证值,供前端像模型 API Key 一样预填并可通过眼睛按钮查看。
        # Web REPL 仅监听 127.0.0.1,且这些值本就来自用户本地的 .cloud-credentials.yml。
        "accessKeyId": credential.access_key_id or None,
        "accessKeySecret": credential.access_key_secret or None,
        "stsToken": credential.sts_token or None,
        "ramRoleArn": credential.ram_role_arn or None,
        "ramSessionName": credential.ram_session_name or None,
    }


def _aliyun_expiration(credential: AliyunCredential) -> int | None:
    if credential.sts_expiration:
        return credential.sts_expiration
    if credential.oauth_access_token_expire:
        return credential.oauth_access_token_expire
    return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_string(data: dict[str, Any], *fields: str) -> str:
    value = _optional_string(data, *fields)
    if value is None:
        raise ValueError("{} is required".format(fields[0]))
    return value


def _optional_string(data: dict[str, Any], *fields: str) -> str | None:
    for field in fields:
        if field not in data:
            continue
        value = data[field]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("{} must be a string".format(field))
        return value
    return None


def _optional_int(data: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        if field not in data:
            continue
        value = data[field]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("{} must be an integer".format(field))
        return value
    return None


def _optional_int_field(data: dict[str, Any], field: str) -> Any:
    """区分三态:字段缺省返回 ``_UNSET``;显式 null 返回 ``None``;否则校验后的 int。"""
    if field not in data:
        return _UNSET
    value = data[field]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("{} must be an integer".format(field))
    return value


def _validate_positive_int(value: Any, name: str) -> None:
    """``_UNSET``/``None`` 放行(保持/清除);其余必须为正整数,否则报错。"""
    if value is _UNSET or value is None:
        return
    if value <= 0:
        raise ValueError("{} must be a positive integer".format(name))


def _apply_int_key(entry: dict[str, Any], key: str, value: Any) -> None:
    """按三态语义写入 provider 配置项:保持 / 清除 / 覆盖。"""
    if value is _UNSET:
        return
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value


def _apply_model_int_knobs(
    entry: dict[str, Any],
    model: str,
    *,
    thinking_budget: Any,
    max_completion_tokens: Any,
) -> None:
    """按模型把两个数值旋钮写入 providers.<key>.models.<model> 下。

    沿用 _apply_int_key 的三态语义(_UNSET 保持 / None 清除 / 正整数覆盖),仅触碰该模型
    条目里的这两个键,保留其它模型与该模型的其它键;写完清理空的 models.<model> 与空 models,
    避免残留空字典。
    """
    models_raw = entry.get("models")
    models_map: dict[str, Any] = dict(models_raw) if isinstance(models_raw, dict) else {}
    model_raw = models_map.get(model)
    model_entry: dict[str, Any] = dict(model_raw) if isinstance(model_raw, dict) else {}
    _apply_int_key(model_entry, "thinkingBudget", thinking_budget)
    _apply_int_key(model_entry, "maxCompletionTokens", max_completion_tokens)
    if model_entry:
        models_map[model] = model_entry
    else:
        models_map.pop(model, None)
    if models_map:
        entry["models"] = models_map
    else:
        entry.pop("models", None)


def _int_or_none(value: Any) -> int | None:
    """回填 payload 用:仅接受真正的 int(排除 bool),否则视为缺省。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _load_existing_aliyun_credential(*, strict: bool) -> AliyunCredential | None:
    try:
        return AliyunCredentials._load_from_iac_code_config()
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(_("stored aliyun credentials are invalid")) from exc
        return None


def _has_any_field(data: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(field in data for field in fields)


def _merged_string(
    data: dict[str, Any],
    existing: AliyunCredential | None,
    attribute: str,
    *fields: str,
    default: str = "",
) -> str:
    if _has_any_field(data, fields):
        return _optional_string(data, *fields) or ""
    if existing is None:
        return default
    value = getattr(existing, attribute, default)
    return value if isinstance(value, str) else default


def _merged_int(
    data: dict[str, Any],
    existing: AliyunCredential | None,
    attribute: str,
    *fields: str,
    default: int = 0,
) -> int:
    if _has_any_field(data, fields):
        return _optional_int(data, *fields) or 0
    if existing is None:
        return default
    value = getattr(existing, attribute, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _validate_aliyun_credential(credential: AliyunCredential) -> None:
    missing = _missing_aliyun_fields(credential)
    if missing:
        raise ValueError("missing required aliyun credential fields: {}".format(", ".join(missing)))


def _is_aliyun_credential_configured(credential: AliyunCredential) -> bool:
    return not _missing_aliyun_fields(credential)


def _missing_aliyun_fields(credential: AliyunCredential) -> list[str]:
    required_by_mode = {
        "AK": ("access_key_id", "access_key_secret"),
        "StsToken": ("access_key_id", "access_key_secret", "sts_token"),
        "RamRoleArn": ("access_key_id", "access_key_secret", "ram_role_arn"),
        "OAuth": ("oauth_site_type", "oauth_access_token", "oauth_refresh_token"),
    }
    required = required_by_mode.get(credential.mode, ())
    return [field for field in required if not str(getattr(credential, field, "") or "").strip()]


def _prune_aliyun_credential_for_mode(credential: AliyunCredential) -> None:
    if credential.mode == "AK":
        credential.sts_token = ""
        credential.sts_expiration = 0
        credential.ram_role_arn = ""
        credential.ram_session_name = ""
        credential.oauth_site_type = ""
        credential.oauth_access_token = ""
        credential.oauth_refresh_token = ""
        credential.oauth_access_token_expire = 0
        credential.oauth_refresh_token_expire = 0
    elif credential.mode == "StsToken":
        credential.ram_role_arn = ""
        credential.ram_session_name = ""
        credential.oauth_site_type = ""
        credential.oauth_access_token = ""
        credential.oauth_refresh_token = ""
        credential.oauth_access_token_expire = 0
        credential.oauth_refresh_token_expire = 0
    elif credential.mode == "RamRoleArn":
        credential.sts_token = ""
        credential.sts_expiration = 0
        credential.oauth_site_type = ""
        credential.oauth_access_token = ""
        credential.oauth_refresh_token = ""
        credential.oauth_access_token_expire = 0
        credential.oauth_refresh_token_expire = 0
    elif credential.mode == "OAuth":
        credential.access_key_id = ""
        credential.access_key_secret = ""
        credential.sts_token = ""
        credential.sts_expiration = 0
        credential.ram_role_arn = ""
        credential.ram_session_name = ""
