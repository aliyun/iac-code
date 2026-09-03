"""Precise DashScope endpoint classification shared by routing and cache gates."""

from __future__ import annotations

from urllib.parse import urlsplit

DASHSCOPE_WIRE_PROVIDER_KEYS = frozenset(
    {"dashscope", "dashscope_token_plan", "aliyun_codingplan", "aliyun_codingplan_intl"}
)

_STANDARD_HOSTS = frozenset(
    {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "cn-hongkong.dashscope.aliyuncs.com",
    }
)
_CODING_HOST_TO_KEY = {
    "coding.dashscope.aliyuncs.com": "aliyun_codingplan",
    "coding-intl.dashscope.aliyuncs.com": "aliyun_codingplan_intl",
}
_TOKEN_PLAN_REGIONS = frozenset({"cn-beijing", "ap-southeast-1", "ap-northeast-1", "eu-central-1", "us-east-1"})


def _parsed_https_endpoint(base_url: str | None) -> tuple[str, str] | None:
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    try:
        parsed = urlsplit(base_url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not hostname or port not in (None, 443):
        return None
    return hostname.lower().rstrip("."), parsed.path.rstrip("/")


def _path_matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def official_dashscope_wire_provider_key(base_url: str | None) -> str | None:
    """Classify official OpenAI-compatible endpoints without substring matches."""
    parsed = _parsed_https_endpoint(base_url)
    if parsed is None:
        return None
    host, path = parsed
    if host in _STANDARD_HOSTS and _path_matches(path, "/compatible-mode"):
        return "dashscope"
    coding_key = _CODING_HOST_TO_KEY.get(host)
    if coding_key is not None and _path_matches(path, "/v1"):
        return coding_key
    labels = host.split(".")
    if (
        len(labels) == 5
        and labels[0] == "token-plan"
        and labels[1] in _TOKEN_PLAN_REGIONS
        and labels[2:] == ["maas", "aliyuncs", "com"]
        and _path_matches(path, "/compatible-mode")
    ):
        return "dashscope_token_plan"
    return None


def is_bailian_compatible_endpoint(base_url: str | None) -> bool:
    """Broad telemetry predicate for official Bailian-compatible endpoints."""
    parsed = _parsed_https_endpoint(base_url)
    if parsed is None:
        return False
    host, path = parsed
    if host in _CODING_HOST_TO_KEY:
        return _path_matches(path, "/v1") or _path_matches(path, "/apps/anthropic")
    if host in _STANDARD_HOSTS:
        return _path_matches(path, "/compatible-mode") or _path_matches(path, "/apps/anthropic")
    labels = host.split(".")
    is_maas = (
        len(labels) == 5
        and labels[2:] == ["maas", "aliyuncs", "com"]
        and _is_dns_label(labels[0])
        and labels[1] in _TOKEN_PLAN_REGIONS
    )
    return is_maas and (_path_matches(path, "/compatible-mode") or _path_matches(path, "/apps/anthropic"))


def _is_dns_label(value: str) -> bool:
    return (
        1 <= len(value) <= 63
        and value[0] != "-"
        and value[-1] != "-"
        and all(character == "-" or "a" <= character <= "z" or "0" <= character <= "9" for character in value)
    )
