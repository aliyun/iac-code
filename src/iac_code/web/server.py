"""Server entry point for the local Web workbench."""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from iac_code.i18n import _, resolve_ui_language, set_language
from iac_code.web.security import ensure_loopback_host
from iac_code.web.settings import get_ui_language

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8766
BROWSER_READY_TIMEOUT_SECONDS = 10.0


def _open_browser_when_ready(
    url: str,
    host: str,
    port: int,
    *,
    timeout_seconds: float = BROWSER_READY_TIMEOUT_SECONDS,
    opener: Callable[[str], object] = webbrowser.open,
) -> None:
    """Open the browser only after the loopback listener accepts a connection."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            connection = socket.create_connection((host, port), timeout=0.2)
        except OSError:
            time.sleep(0.05)
            continue
        connection.close()
        opener(url)
        return


def _render_startup_banner(url: str) -> str:
    """Build a friendly startup banner highlighting the clickable local URL."""
    title = _("IaC Code Web is running")
    url_line = _("Local:  {}").format(url)
    stop_line = _("Press Ctrl+C to stop")
    body = [title, "→  {}".format(url_line), stop_line]
    width = max(len(line) for line in body) + 4
    rule = "━" * width
    padded = "\n".join("  {}".format(line) for line in body)
    return "\n{}\n{}\n{}\n".format(rule, padded, rule)


def _schedule_browser_open(url: str, host: str, port: int) -> None:
    threading.Thread(
        target=_open_browser_when_ready,
        args=(url, host, port),
        name="iac-code-web-browser-open",
        daemon=True,
    ).start()


def _reexec_argv(argv: list[str]) -> list[str]:
    """Rewrite the launch argv so the fresh process does not auto-open a browser.

    A restart re-runs the original launch command. Since --open now defaults on,
    an unmodified replay would pop a second browser tab on top of the page the
    frontend already reloads, so force --no-open (dropping any existing open flag).
    """
    filtered = [arg for arg in argv if arg not in ("--open", "--no-open")]
    filtered.append("--no-open")
    return filtered


def _default_reexec() -> None:
    """Replace the current process with a fresh invocation (no second browser tab)."""
    argv = _reexec_argv(list(sys.orig_argv))
    os.execv(argv[0], argv)


def _default_shutdown() -> None:
    from iac_code.services.telemetry import graceful_shutdown

    graceful_shutdown()


def _start_update_check(
    *,
    start_fn: Callable[..., object] | None = None,
    interval_seconds: float | None = None,
) -> None:
    """在后台线程周期检查 PyPI 更新(尊重 2h 节流与 dev-build gate),绝不阻塞或崩溃启动。

    web 是长驻进程,启动检一次不够;interval_seconds 默认取 6h,启动即进入周期模式。
    """
    from iac_code import __version__
    from iac_code.services.update_checker import (
        WEB_UPDATE_CHECK_INTERVAL_SECONDS,
        start_background_update_check,
    )

    if interval_seconds is None:
        interval_seconds = WEB_UPDATE_CHECK_INTERVAL_SECONDS
    do_start = start_fn or start_background_update_check
    try:
        do_start(current_version=__version__, interval_seconds=interval_seconds)
    except Exception:  # pragma: no cover - 后台检查失败不得影响服务启动
        pass


def schedule_restart(
    *,
    delay: float = 0.4,
    exec_fn: Callable[[], object] | None = None,
    shutdown_fn: Callable[[], object] | None = None,
    timer_factory: Callable[[float, Callable[[], object]], Any] = threading.Timer,
) -> Any:
    """Arm an in-process restart: flush telemetry then re-exec after a short delay.

    The delay lets the triggering HTTP response flush before os.execv replaces the
    process image (which bypasses the CLI's ``finally: graceful_shutdown()``).
    """
    do_exec = exec_fn or _default_reexec
    do_shutdown = shutdown_fn or _default_shutdown

    def _run() -> None:
        do_shutdown()
        do_exec()

    timer = timer_factory(delay, _run)
    setattr(timer, "daemon", True)
    timer.start()
    return timer


def _header_values(scope: dict[str, Any], name: bytes) -> list[str]:
    return [value.decode("latin-1").strip() for key, value in scope.get("headers", []) if key.lower() == name]


def _authority_hostname(authority: str) -> str | None:
    if not authority or "@" in authority:
        return None
    try:
        parsed = urlsplit("//{}".format(authority))
        _ = parsed.port
    except ValueError:
        return None
    return parsed.hostname


def _is_allowed_loopback_name(hostname: str | None) -> bool:
    if hostname is None:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_matches_authority(origin: str, *, authority: str, scope_scheme: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit("//{}".format(authority))
        origin_port = parsed_origin.port
        host_port = parsed_host.port
    except ValueError:
        return False
    expected_scheme = {"ws": "http", "wss": "https"}.get(scope_scheme, scope_scheme)
    if expected_scheme not in {"http", "https"} or parsed_origin.scheme != expected_scheme:
        return False
    if parsed_origin.username is not None or parsed_origin.password is not None:
        return False
    origin_hostname = (parsed_origin.hostname or "").rstrip(".").lower()
    host_hostname = (parsed_host.hostname or "").rstrip(".").lower()
    if not _is_allowed_loopback_name(origin_hostname) or origin_hostname != host_hostname:
        return False
    default_port = 443 if expected_scheme == "https" else 80
    return (origin_port or default_port) == (host_port or default_port)


class _LoopbackRequestGuard:
    """Reject browser requests that could reach loopback through DNS rebinding."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        hosts = _header_values(scope, b"host")
        origins = _header_values(scope, b"origin")
        fetch_sites = _header_values(scope, b"sec-fetch-site")
        valid = (
            len(hosts) == 1
            and _is_allowed_loopback_name(_authority_hostname(hosts[0]))
            and len(origins) <= 1
            and (
                not origins
                or _origin_matches_authority(
                    origins[0],
                    authority=hosts[0],
                    scope_scheme=str(scope.get("scheme") or "http"),
                )
            )
            and len(fetch_sites) <= 1
            and (not fetch_sites or fetch_sites[0].lower() in {"same-origin", "same-site", "none"})
        )
        if valid:
            await self.app(scope, receive, send)
            return

        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Forbidden"})
            return
        body = b"Forbidden"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", b"9")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def protect_loopback_app(app: Any) -> Any:
    return _LoopbackRequestGuard(app)


def run_web_server(
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    open_browser: bool = False,
) -> None:
    """Run the local Web workbench."""
    safe_host = ensure_loopback_host(host)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
        message = _("Web server dependencies are missing. Install with: pip install 'iac-code[http]'")
        raise RuntimeError(message) from exc

    from iac_code.web.app import create_app

    # 单用户本地进程:按持久化的 UI 语言重绑定进程级 gettext,使后端 _() 与前端 UI 语言一致。
    set_language(resolve_ui_language(get_ui_language()))

    browser_host = "[{}]".format(safe_host) if ":" in safe_host else safe_host
    url = "http://{}:{}".format(browser_host, port)
    print(_render_startup_banner(url), flush=True)
    _start_update_check()
    if open_browser:
        _schedule_browser_open(url, safe_host, port)
    # 该服务只绑定 loopback，转录里展示真实本地路径既安全又有用，故关闭文件路径脱敏
    # (密钥等敏感值仍会脱敏)。见 create_app(expose_local_paths=...)。
    uvicorn.run(protect_loopback_app(create_app(expose_local_paths=True)), host=safe_host, port=port)
