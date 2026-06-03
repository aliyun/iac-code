import base64
import hashlib
import queue
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from iac_code.i18n import _

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/cli/callback"
CALLBACK_PORTS = tuple(range(12345, 12350))
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 300
ACCESS_TOKEN_SKEW_SECONDS = 60
STS_SKEW_SECONDS = 120
PERMANENT_OAUTH_ERROR_CODES = {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_token"}


@dataclass(frozen=True)
class AliyunOAuthSite:
    site_type: str
    display_name: str
    client_id: str
    signin_base_url: str
    oauth_base_url: str


OAUTH_SITES: dict[str, AliyunOAuthSite] = {
    "CN": AliyunOAuthSite(
        site_type="CN",
        display_name="China",
        client_id="4038181954557748008",
        signin_base_url="https://signin.aliyun.com",
        oauth_base_url="https://oauth.aliyun.com",
    ),
    "INTL": AliyunOAuthSite(
        site_type="INTL",
        display_name="International",
        client_id="4103531455503354461",
        signin_base_url="https://signin.alibabacloud.com",
        oauth_base_url="https://oauth.alibabacloud.com",
    ),
}


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    access_token_expire: int
    refresh_token_expire: int = 0


@dataclass(frozen=True)
class OAuthStsCredentials:
    access_key_id: str
    access_key_secret: str
    sts_token: str
    sts_expiration: int


class AliyunOAuthError(RuntimeError):
    def __init__(self, message: str, *, error_code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class AliyunOAuthCancelledError(AliyunOAuthError):
    pass


def oauth_relogin_hint() -> str:
    return _("Run /auth and choose OAuth Login (Browser).")


class AliyunOAuthReloginRequired(AliyunOAuthError):  # noqa: N818
    def __init__(self, message: str, *, error_code: str | None = None, status_code: int | None = None) -> None:
        hint = oauth_relogin_hint()
        if hint not in message:
            message = "{} {}".format(message, hint)
        super().__init__(message, error_code=error_code, status_code=status_code)


def get_oauth_site(site_type: str) -> AliyunOAuthSite:
    normalized = site_type.strip().lower()
    aliases = {
        "cn": "CN",
        "china": "CN",
        "aliyun": "CN",
        "intl": "INTL",
        "international": "INTL",
        "alibabacloud": "INTL",
    }
    site_key = aliases.get(normalized)
    if not site_key:
        raise AliyunOAuthError(_("Unknown Aliyun OAuth site: {site_type}").format(site_type=site_type))
    return OAUTH_SITES[site_key]


def oauth_site_options() -> list[tuple[str, str]]:
    return [("CN", "China"), ("INTL", "International")]


def generate_state() -> str:
    return secrets.token_urlsafe(16)


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(96)[:128]


def generate_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(site: AliyunOAuthSite, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": site.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return "{}/oauth2/v1/auth?{}".format(site.signin_base_url.rstrip("/"), query)


class OAuthCallbackServer:
    def __init__(
        self,
        ports: tuple[int, ...] = CALLBACK_PORTS,
        timeout_seconds: int = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    ) -> None:
        self.ports = ports
        self.timeout_seconds = timeout_seconds
        self.redirect_uri = ""
        self._results: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, expected_state: str) -> None:
        if self._server is not None:
            return

        result_queue = self._results

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed_url = urlparse(self.path)
                if parsed_url.path != CALLBACK_PATH:
                    self._send_plain_response(404, _("Not found"))
                    return

                query = parse_qs(parsed_url.query)
                state = query.get("state", [""])[0]
                if state != expected_state:
                    self._put_result(("error", _("invalid state")))
                    self._send_plain_response(400, _("Invalid state"))
                    return

                code = query.get("code", [""])[0]
                if not code:
                    self._put_result(("error", _("code not found")))
                    self._send_plain_response(400, _("Authorization code not found"))
                    return

                self._put_result(("code", code))
                self._send_plain_response(200, _("Authorization successful. You can close this window."))

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _put_result(self, result: tuple[str, str]) -> None:
                try:
                    result_queue.put_nowait(result)
                except queue.Full:
                    return

            def _send_plain_response(self, status_code: int, body: str) -> None:
                body_bytes = body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

        last_error: OSError | None = None
        for port in self.ports:
            try:
                self._server = ThreadingHTTPServer((CALLBACK_HOST, port), CallbackHandler)
            except OSError as exc:
                last_error = exc
                continue
            self.redirect_uri = "http://{}:{}{}".format(CALLBACK_HOST, port, CALLBACK_PATH)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            return

        message = _("No available callback port in range {start}-{end}").format(
            start=self.ports[0],
            end=self.ports[-1],
        )
        raise AliyunOAuthError(message) from last_error

    def wait_for_code(
        self,
        *,
        cancel_event: threading.Event | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AliyunOAuthCancelledError(_("OAuth login cancelled."))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AliyunOAuthError(oauth_callback_timeout_message())

            try:
                result_type, value = self._results.get(timeout=min(poll_interval_seconds, remaining))
                break
            except queue.Empty:
                continue

        if result_type == "error":
            raise AliyunOAuthError(value)
        return value

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=1)


def run_browser_oauth_flow(
    site_type: str,
    *,
    oauth_client: Any | None = None,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    callback_server_factory: Callable[[], Any] | None = None,
    writer: Callable[[str], None] = print,
    cancel_event: threading.Event | None = None,
    now: int | None = None,
) -> OAuthToken:
    site = get_oauth_site(site_type)
    client = oauth_client or AliyunOAuthClient(site)
    server = callback_server_factory() if callback_server_factory is not None else OAuthCallbackServer()
    state = generate_state()
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)

    try:
        server.start(state)
        url = build_authorization_url(site, server.redirect_uri, state, code_challenge)
        for line in oauth_browser_login_guidance():
            writer(line)
        writer("  {}".format(_("Open in your browser:")))
        writer("  {}".format(url))
        try:
            browser_opener(url)
        except Exception:
            pass

        code = server.wait_for_code(cancel_event=cancel_event) if cancel_event is not None else server.wait_for_code()
        return client.exchange_code_for_token(
            code=code,
            redirect_uri=server.redirect_uri,
            code_verifier=code_verifier,
            now=now,
        )
    finally:
        server.close()


def oauth_browser_login_guidance() -> list[str]:
    messages = [
        "",
        _("Waiting for browser authorization"),
        _("1. The browser may show official-cli; this is the Alibaba Cloud official CLI OAuth application."),
        _(
            "2. If assignment is required, assign the RAM user or RAM role that is signed in. "
            "User groups are not supported."
        ),
        _(
            "3. After assignment, close the old authorization page and run OAuth Login (Browser) again. "
            "If it still fails, sign out of Alibaba Cloud and sign in again."
        ),
        _(
            "4. STS credentials refresh when possible until Alibaba Cloud expires them. "
            "If refresh fails, run /auth again."
        ),
        _("Press Esc to cancel while waiting."),
        "",
    ]
    return ["" if not message else "  {}".format(message) for message in messages]


def oauth_callback_timeout_message() -> str:
    return _(
        "Timed out waiting for OAuth callback. If Alibaba Cloud asked you to assign the official-cli application, "
        "assign it to the exact RAM user or RAM role currently signed in. User groups are not supported. "
        "Then close the old authorization page, sign out of Alibaba Cloud and sign in again if needed, "
        "and run /auth to choose OAuth Login (Browser) again."
    )


def is_epoch_expired(expiration: int, now: int | None = None, skew_seconds: int = 0) -> bool:
    if expiration <= 0:
        return True
    current_time = int(time.time()) if now is None else now
    return expiration <= current_time + skew_seconds


def parse_sts_exchange_response(data: dict[str, Any]) -> OAuthStsCredentials:
    access_key_id = _first_present(data, "accessKeyId", "AccessKeyId")
    access_key_secret = _first_present(data, "accessKeySecret", "AccessKeySecret")
    sts_token = _first_present(data, "securityToken", "SecurityToken")
    expiration = _first_present(data, "expiration", "Expiration")

    missing = [
        name
        for name, value in (
            ("accessKeyId", access_key_id),
            ("accessKeySecret", access_key_secret),
            ("securityToken", sts_token),
            ("expiration", expiration),
        )
        if value in (None, "")
    ]
    if missing:
        raise AliyunOAuthError("STS exchange response missing required field(s): {}".format(", ".join(missing)))

    return OAuthStsCredentials(
        access_key_id=str(access_key_id),
        access_key_secret=str(access_key_secret),
        sts_token=str(sts_token),
        sts_expiration=_parse_expiration(expiration),
    )


class AliyunOAuthClient:
    def __init__(self, site: AliyunOAuthSite, http_client: httpx.Client | None = None) -> None:
        self.site = site
        self.http_client = http_client or httpx.Client(timeout=30.0)

    def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        now: int | None = None,
    ) -> OAuthToken:
        response = self._post(
            "{}/v1/token".format(self.site.oauth_base_url.rstrip("/")),
            "exchange authorization code for token",
            sensitive_values=(code, code_verifier),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.site.client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        self._raise_for_oauth_error(
            response,
            "exchange authorization code for token",
            sensitive_values=(code, code_verifier),
        )
        data = self._json_response(response, "exchange authorization code for token")
        return self._parse_token_response(
            data,
            operation="exchange authorization code for token",
            fallback_refresh_token=None,
            now=now,
        )

    def refresh_access_token(self, refresh_token: str, now: int | None = None) -> OAuthToken:
        response = self._post(
            "{}/v1/token".format(self.site.oauth_base_url.rstrip("/")),
            "refresh access token",
            sensitive_values=(refresh_token,),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.site.client_id,
            },
        )
        self._raise_for_oauth_error(response, "refresh access token", sensitive_values=(refresh_token,))
        data = self._json_response(response, "refresh access token")
        return self._parse_token_response(
            data,
            operation="refresh access token",
            fallback_refresh_token=refresh_token,
            now=now,
        )

    def exchange_access_token_for_sts(self, access_token: str) -> OAuthStsCredentials:
        response = self._post(
            "{}/v1/exchange".format(self.site.oauth_base_url.rstrip("/")),
            "exchange access token for STS",
            sensitive_values=(access_token,),
            headers={"Authorization": "Bearer {}".format(access_token), "Content-Type": "application/json"},
            json={},
        )
        self._raise_for_oauth_error(
            response,
            "exchange access token for STS",
            sensitive_values=(access_token,),
        )
        data = self._json_response(response, "exchange access token for STS")
        return parse_sts_exchange_response(data)

    def _parse_token_response(
        self,
        data: dict[str, Any],
        *,
        operation: str,
        fallback_refresh_token: str | None,
        now: int | None,
    ) -> OAuthToken:
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token") or fallback_refresh_token
        expires_in = data.get("expires_in")
        missing = [
            name
            for name, value in (
                ("access_token", access_token),
                ("refresh_token", refresh_token),
                ("expires_in", expires_in),
            )
            if value in (None, "")
        ]
        if missing:
            raise AliyunOAuthError("{} response missing required field(s): {}".format(operation, ", ".join(missing)))

        current_time = int(time.time()) if now is None else now
        access_token_expire = current_time + _parse_int(expires_in, "expires_in")
        refresh_expires_in = data.get("refresh_expires_in")
        refresh_token_expire = 0
        if refresh_expires_in not in (None, ""):
            refresh_token_expire = current_time + _parse_int(refresh_expires_in, "refresh_expires_in")

        return OAuthToken(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            access_token_expire=access_token_expire,
            refresh_token_expire=refresh_token_expire,
        )

    def _raise_for_oauth_error(
        self,
        response: httpx.Response,
        operation: str,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        if response.status_code == 200:
            return

        body = _response_json_or_empty(response)
        error_code = _string_or_none(body.get("error"))
        error_description = _redact_sensitive_values(_string_or_none(body.get("error_description")), sensitive_values)
        message_parts = ["{} failed with status {}".format(operation, response.status_code)]
        if error_code:
            message_parts.append("error={}".format(error_code))
        if error_description:
            message_parts.append("error_description={}".format(error_description))
        if len(message_parts) > 1:
            message = ": ".join([message_parts[0], ", ".join(message_parts[1:])])
        else:
            message = message_parts[0]

        error_cls = AliyunOAuthReloginRequired if error_code in PERMANENT_OAUTH_ERROR_CODES else AliyunOAuthError
        raise error_cls(message, error_code=error_code, status_code=response.status_code)

    def _post(
        self,
        url: str,
        operation: str,
        *,
        sensitive_values: tuple[str, ...],
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return self.http_client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            detail = _redact_sensitive_values(str(exc), sensitive_values) or exc.__class__.__name__
            raise AliyunOAuthError("{} request failed: {}".format(operation, detail)) from exc

    def _json_response(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AliyunOAuthError(
                "{} response was not valid JSON".format(operation),
                status_code=response.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise AliyunOAuthError(
                "{} response JSON was not an object".format(operation),
                status_code=response.status_code,
            )
        return data


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _parse_expiration(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        if stripped.endswith("Z"):
            stripped = "{}+00:00".format(stripped[:-1])
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError as exc:
            raise AliyunOAuthError("STS exchange response has invalid expiration") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    raise AliyunOAuthError("STS exchange response has invalid expiration")


def _parse_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AliyunOAuthError("OAuth token response has invalid {}".format(field_name)) from exc


def _response_json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _redact_sensitive_values(value: str | None, sensitive_values: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    redacted = value
    for sensitive_value in sensitive_values:
        if sensitive_value:
            redacted = redacted.replace(sensitive_value, "[REDACTED]")
    return redacted
