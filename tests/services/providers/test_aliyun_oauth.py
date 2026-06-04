import socket
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import httpx
import pytest

from iac_code.services.providers.aliyun_oauth import (
    AliyunOAuthCancelledError,
    AliyunOAuthClient,
    AliyunOAuthError,
    AliyunOAuthReloginRequired,
    OAuthCallbackServer,
    OAuthStsCredentials,
    OAuthToken,
    build_authorization_url,
    generate_code_challenge,
    get_oauth_site,
    is_epoch_expired,
    parse_sts_exchange_response,
    run_browser_oauth_flow,
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_get_cn_site_config():
    site = get_oauth_site("China")

    assert site.site_type == "CN"
    assert site.display_name == "China"
    assert site.client_id == "4038181954557748008"
    assert site.signin_base_url == "https://signin.aliyun.com"
    assert site.oauth_base_url == "https://oauth.aliyun.com"


def test_get_intl_site_config_accepts_international_alias():
    site = get_oauth_site("International")

    assert site.site_type == "INTL"
    assert site.display_name == "International"
    assert site.client_id == "4103531455503354461"
    assert site.signin_base_url == "https://signin.alibabacloud.com"
    assert site.oauth_base_url == "https://oauth.alibabacloud.com"


def test_generate_code_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert generate_code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_build_authorization_url_uses_signin_host_and_pkce():
    site = get_oauth_site("CN")
    url = build_authorization_url(
        site,
        redirect_uri="http://127.0.0.1:12345/cli/callback",
        state="fake-state",
        code_challenge="fake-challenge",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "signin.aliyun.com"
    assert parsed.path == "/oauth2/v1/auth"
    assert query == {
        "response_type": ["code"],
        "client_id": ["4038181954557748008"],
        "redirect_uri": ["http://127.0.0.1:12345/cli/callback"],
        "state": ["fake-state"],
        "code_challenge": ["fake-challenge"],
        "code_challenge_method": ["S256"],
    }


def test_callback_server_accepts_matching_state():
    server = OAuthCallbackServer(ports=(_free_loopback_port(),), timeout_seconds=1)
    server.start("expected-state")

    try:
        with urlopen("{}?state=expected-state&code=auth-code".format(server.redirect_uri), timeout=1) as response:
            body = response.read()

        assert response.status == 200
        assert b"Authorization successful" in body
        assert server.wait_for_code() == "auth-code"
    finally:
        server.close()


def test_callback_server_rejects_invalid_state():
    server = OAuthCallbackServer(ports=(_free_loopback_port(),), timeout_seconds=1)
    server.start("expected-state")

    try:
        with pytest.raises(HTTPError) as exc_info:
            urlopen("{}?state=wrong-state&code=auth-code".format(server.redirect_uri), timeout=1)

        assert exc_info.value.code == 400
        with pytest.raises(AliyunOAuthError, match="invalid state"):
            server.wait_for_code()
    finally:
        server.close()


def test_callback_server_rejects_missing_code():
    server = OAuthCallbackServer(ports=(_free_loopback_port(),), timeout_seconds=1)
    server.start("expected-state")

    try:
        with pytest.raises(HTTPError) as exc_info:
            urlopen("{}?state=expected-state".format(server.redirect_uri), timeout=1)

        assert exc_info.value.code == 400
        with pytest.raises(AliyunOAuthError, match="code not found"):
            server.wait_for_code()
    finally:
        server.close()


def test_callback_server_timeout_includes_assignment_troubleshooting():
    server = OAuthCallbackServer(ports=(_free_loopback_port(),), timeout_seconds=0)

    with pytest.raises(AliyunOAuthError) as exc_info:
        server.wait_for_code()

    message = str(exc_info.value)
    assert "Timed out waiting for OAuth callback" in message
    assert "official-cli" in message
    assert "RAM user" in message
    assert "RAM role" in message
    assert "sign out" in message
    assert "OAuth Login (Browser)" in message


def test_callback_server_wait_for_code_can_be_cancelled():
    server = OAuthCallbackServer(ports=(_free_loopback_port(),), timeout_seconds=30)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(AliyunOAuthCancelledError, match="cancelled"):
        server.wait_for_code(cancel_event=cancel_event)


def test_run_browser_oauth_flow_prints_url_opens_browser_and_exchanges_code():
    opened_urls: list[str] = []
    lines: list[str] = []

    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            assert expected_state

        def wait_for_code(self) -> str:
            return "auth-code"

        def close(self) -> None:
            pass

    class FakeClient:
        def exchange_code_for_token(
            self,
            code: str,
            redirect_uri: str,
            code_verifier: str,
            now: int | None = None,
        ) -> OAuthToken:
            assert code == "auth-code"
            assert redirect_uri == FakeServer.redirect_uri
            assert code_verifier
            assert now == 1000
            return OAuthToken(
                access_token="fake-access",
                refresh_token="fake-refresh",
                access_token_expire=4600,
            )

    def browser_opener(url: str) -> bool:
        opened_urls.append(url)
        return True

    token = run_browser_oauth_flow(
        "CN",
        oauth_client=FakeClient(),
        browser_opener=browser_opener,
        callback_server_factory=FakeServer,
        writer=lines.append,
        now=1000,
    )

    assert token.access_token == "fake-access"
    assert token.refresh_token == "fake-refresh"
    assert opened_urls
    assert parse_qs(urlparse(opened_urls[0]).query)["redirect_uri"] == [FakeServer.redirect_uri]
    assert lines[0] == ""
    assert lines[1] == "  Waiting for browser authorization"
    assert lines[2].startswith("  1. ")
    assert lines[3].startswith("  2. ")
    assert lines[4].startswith("  3. ")
    assert lines[5].startswith("  4. ")
    assert "official-cli" in lines[2]
    assert "RAM user" in lines[3]
    assert "RAM role" in lines[3]
    assert "User groups are not supported" in lines[3]
    assert "Press Esc" in lines[6]
    assert lines[-2] == "  Open in your browser:"
    assert lines[-1].startswith("  https://signin.aliyun.com/oauth2/v1/auth?")
    assert not any("SignIn url" in line for line in lines)


def test_run_browser_oauth_flow_passes_cancel_event_to_callback_wait():
    cancel_event = threading.Event()

    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            assert expected_state

        def wait_for_code(self, *, cancel_event: threading.Event | None = None) -> str:
            assert cancel_event is not None
            assert cancel_event is expected_cancel_event
            return "auth-code"

        def close(self) -> None:
            pass

    expected_cancel_event = cancel_event

    class FakeClient:
        def exchange_code_for_token(
            self,
            code: str,
            redirect_uri: str,
            code_verifier: str,
            now: int | None = None,
        ) -> OAuthToken:
            return OAuthToken(
                access_token="fake-access",
                refresh_token="fake-refresh",
                access_token_expire=4600,
            )

    token = run_browser_oauth_flow(
        "CN",
        oauth_client=FakeClient(),
        browser_opener=lambda url: True,
        callback_server_factory=FakeServer,
        writer=lambda line: None,
        cancel_event=cancel_event,
    )

    assert token.access_token == "fake-access"


def test_run_browser_oauth_flow_continues_when_browser_open_raises():
    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            assert expected_state

        def wait_for_code(self) -> str:
            return "auth-code"

        def close(self) -> None:
            pass

    class FakeClient:
        def exchange_code_for_token(
            self,
            code: str,
            redirect_uri: str,
            code_verifier: str,
            now: int | None = None,
        ) -> OAuthToken:
            assert code == "auth-code"
            assert redirect_uri == FakeServer.redirect_uri
            assert code_verifier
            return OAuthToken(
                access_token="fake-access",
                refresh_token="fake-refresh",
                access_token_expire=4600,
            )

    def browser_opener(url: str) -> bool:
        raise RuntimeError("browser unavailable")

    token = run_browser_oauth_flow(
        "CN",
        oauth_client=FakeClient(),
        browser_opener=browser_opener,
        callback_server_factory=FakeServer,
        writer=lambda line: None,
    )

    assert token.access_token == "fake-access"


def test_run_browser_oauth_flow_closes_internally_created_client_on_success(monkeypatch):
    clients = []

    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            pass

        def wait_for_code(self) -> str:
            return "auth-code"

        def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, site):
            self.closed = False
            clients.append(self)

        def exchange_code_for_token(self, **kwargs) -> OAuthToken:
            return OAuthToken("fake-access", "fake-refresh", 4600)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeClient)

    token = run_browser_oauth_flow(
        "CN",
        browser_opener=lambda url: True,
        callback_server_factory=FakeServer,
        writer=lambda line: None,
    )

    assert token.access_token == "fake-access"
    assert len(clients) == 1
    assert clients[0].closed is True


@pytest.mark.parametrize("error_cls", [AliyunOAuthError, AliyunOAuthCancelledError])
def test_run_browser_oauth_flow_closes_internally_created_client_on_error(monkeypatch, error_cls):
    clients = []

    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            pass

        def wait_for_code(self) -> str:
            raise error_cls("flow failed")

        def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, site):
            self.closed = False
            clients.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeClient)

    with pytest.raises(error_cls):
        run_browser_oauth_flow(
            "CN",
            browser_opener=lambda url: True,
            callback_server_factory=FakeServer,
            writer=lambda line: None,
        )

    assert len(clients) == 1
    assert clients[0].closed is True


def test_run_browser_oauth_flow_closes_internally_created_client_when_server_factory_raises(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, site):
            self.closed = False
            clients.append(self)

        def close(self) -> None:
            self.closed = True

    def failing_server_factory():
        raise RuntimeError("callback setup failed")

    monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeClient)

    with pytest.raises(RuntimeError, match="callback setup failed"):
        run_browser_oauth_flow(
            "CN",
            callback_server_factory=failing_server_factory,
            writer=lambda line: None,
        )

    assert len(clients) == 1
    assert clients[0].closed is True


def test_run_browser_oauth_flow_leaves_injected_client_open_when_server_factory_raises():
    class FakeClient:
        closed = False

        def close(self) -> None:
            self.closed = True

    def failing_server_factory():
        raise RuntimeError("callback setup failed")

    client = FakeClient()

    with pytest.raises(RuntimeError, match="callback setup failed"):
        run_browser_oauth_flow(
            "CN",
            oauth_client=client,
            callback_server_factory=failing_server_factory,
            writer=lambda line: None,
        )

    assert client.closed is False


def test_run_browser_oauth_flow_leaves_injected_client_open():
    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            pass

        def wait_for_code(self) -> str:
            return "auth-code"

        def close(self) -> None:
            pass

    class FakeClient:
        closed = False

        def exchange_code_for_token(self, **kwargs) -> OAuthToken:
            return OAuthToken("fake-access", "fake-refresh", 4600)

        def close(self) -> None:
            self.closed = True

    client = FakeClient()

    run_browser_oauth_flow(
        "CN",
        oauth_client=client,
        browser_opener=lambda url: True,
        callback_server_factory=FakeServer,
        writer=lambda line: None,
    )

    assert client.closed is False


def test_run_browser_oauth_flow_uses_falsey_injected_client_without_closing_it(monkeypatch):
    class FakeServer:
        redirect_uri = "http://127.0.0.1:12345/cli/callback"

        def start(self, expected_state: str) -> None:
            pass

        def wait_for_code(self) -> str:
            return "auth-code"

        def close(self) -> None:
            pass

    class FalseyClient:
        closed = False
        exchanged = False

        def __bool__(self):
            return False

        def exchange_code_for_token(self, **kwargs) -> OAuthToken:
            assert kwargs["code"] == "auth-code"
            self.exchanged = True
            return OAuthToken("fake-access", "fake-refresh", 4600)

        def close(self) -> None:
            self.closed = True

    def fail_internal_client(site):
        raise AssertionError("internal OAuth client should not be constructed")

    client = FalseyClient()
    monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", fail_internal_client)

    token = run_browser_oauth_flow(
        "CN",
        oauth_client=client,
        browser_opener=lambda url: True,
        callback_server_factory=FakeServer,
        writer=lambda line: None,
    )

    assert token.access_token == "fake-access"
    assert client.exchanged is True
    assert client.closed is False


def test_parse_sts_exchange_response_accepts_camel_case():
    credentials = parse_sts_exchange_response(
        {
            "accessKeyId": "fake-ak",
            "accessKeySecret": "fake-secret",
            "securityToken": "fake-sts",
            "expiration": "2026-01-01T01:00:00Z",
        }
    )

    assert credentials == OAuthStsCredentials(
        access_key_id="fake-ak",
        access_key_secret="fake-secret",
        sts_token="fake-sts",
        sts_expiration=1767229200,
    )


def test_parse_sts_exchange_response_accepts_pascal_case():
    credentials = parse_sts_exchange_response(
        {
            "AccessKeyId": "fake-ak",
            "AccessKeySecret": "fake-secret",
            "SecurityToken": "fake-sts",
            "Expiration": "1767229200",
        }
    )

    assert credentials == OAuthStsCredentials(
        access_key_id="fake-ak",
        access_key_secret="fake-secret",
        sts_token="fake-sts",
        sts_expiration=1767229200,
    )


def test_is_epoch_expired_uses_skew():
    assert is_epoch_expired(1061, now=1000, skew_seconds=60) is False
    assert is_epoch_expired(1060, now=1000, skew_seconds=60) is True
    assert is_epoch_expired(0, now=1000, skew_seconds=60) is True


def test_exchange_code_for_token_posts_authorization_code_form():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "fake-access",
                "refresh_token": "fake-refresh",
                "expires_in": 3600,
                "refresh_expires_in": 86400,
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        token = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client).exchange_code_for_token(
            code="fake-code",
            redirect_uri="http://127.0.0.1:12345/cli/callback",
            code_verifier="fake-verifier",
            now=1000,
        )

    assert token.access_token == "fake-access"
    assert token.refresh_token == "fake-refresh"
    assert token.access_token_expire == 4600
    assert token.refresh_token_expire == 87400
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://oauth.aliyun.com/v1/token"
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": ["fake-code"],
        "client_id": ["4038181954557748008"],
        "redirect_uri": ["http://127.0.0.1:12345/cli/callback"],
        "code_verifier": ["fake-verifier"],
    }


def test_oauth_client_close_only_closes_internally_owned_http_client():
    owned_client = AliyunOAuthClient(get_oauth_site("CN"))

    owned_client.close()
    owned_client.close()

    assert owned_client.http_client.is_closed is True

    external_http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        external_client = AliyunOAuthClient(get_oauth_site("CN"), http_client=external_http_client)

        external_client.close()

        assert external_http_client.is_closed is False
    finally:
        external_http_client.close()


def test_oauth_client_uses_falsey_injected_http_client_without_closing_it(monkeypatch):
    class FalseyHttpClient:
        closed = False

        def __bool__(self):
            return False

        def close(self) -> None:
            self.closed = True

    def fail_internal_http_client(*args, **kwargs):
        raise AssertionError("internal httpx client should not be constructed")

    http_client = FalseyHttpClient()
    monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.httpx.Client", fail_internal_http_client)

    client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)

    assert client.http_client is http_client
    client.close()
    assert http_client.closed is False


def test_oauth_client_context_manager_closes_owned_http_client():
    with AliyunOAuthClient(get_oauth_site("CN")) as client:
        http_client = client.http_client
        assert http_client.is_closed is False

    assert http_client.is_closed is True


def test_refresh_access_token_preserves_existing_refresh_token_when_response_omits_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["existing-refresh"],
            "client_id": ["4038181954557748008"],
        }
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "expires_in": 1800,
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        token = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client).refresh_access_token(
            "existing-refresh",
            now=2000,
        )

    assert token.access_token == "new-access"
    assert token.refresh_token == "existing-refresh"
    assert token.access_token_expire == 3800
    assert token.refresh_token_expire == 0


def test_exchange_access_token_for_sts_sends_bearer_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake-access"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(
            200,
            json={
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "securityToken": "fake-sts",
                "expiration": 1767229200,
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        credentials = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client).exchange_access_token_for_sts(
            "fake-access"
        )

    assert credentials == OAuthStsCredentials(
        access_key_id="fake-ak",
        access_key_secret="fake-secret",
        sts_token="fake-sts",
        sts_expiration=1767229200,
    )


def test_permanent_oauth_error_raises_relogin_required():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "authorization code expired"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthReloginRequired) as exc_info:
            client.refresh_access_token("fake-refresh")

    assert exc_info.value.error_code == "invalid_grant"
    assert exc_info.value.status_code == 400
    assert "refresh access token failed with status 400" in str(exc_info.value)
    assert "invalid_grant" in str(exc_info.value)
    assert "authorization code expired" in str(exc_info.value)
    assert "/auth" in str(exc_info.value)
    assert "OAuth Login (Browser)" in str(exc_info.value)
    assert "fake-refresh" not in str(exc_info.value)


def test_refresh_access_token_redacts_refresh_token_from_error_description():
    refresh_token = "refresh-token-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "refresh token refresh-token-secret is expired",
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthReloginRequired) as exc_info:
            client.refresh_access_token(refresh_token)

    message = str(exc_info.value)
    assert "[REDACTED]" in message
    assert refresh_token not in message
    assert "refresh token" in message
    assert "is expired" in message
    assert "/auth" in message
    assert "OAuth Login (Browser)" in message


def test_exchange_access_token_for_sts_redacts_access_token_from_error_description():
    access_token = "access-token-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "error": "server_error",
                "error_description": "bearer access-token-secret failed validation",
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthError) as exc_info:
            client.exchange_access_token_for_sts(access_token)

    message = str(exc_info.value)
    assert "[REDACTED]" in message
    assert access_token not in message
    assert "bearer" in message
    assert "failed validation" in message


def test_non_permanent_oauth_error_raises_oauth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": "server_error", "error_description": "temporary outage"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthError) as exc_info:
            client.exchange_access_token_for_sts("fake-access")

    assert not isinstance(exc_info.value, AliyunOAuthReloginRequired)
    assert exc_info.value.error_code == "server_error"
    assert exc_info.value.status_code == 500
    assert "exchange access token for STS failed with status 500" in str(exc_info.value)
    assert "server_error" in str(exc_info.value)
    assert "temporary outage" in str(exc_info.value)
    assert "fake-access" not in str(exc_info.value)


def test_exchange_code_for_token_wraps_http_error_and_redacts_sensitive_values():
    auth_code = "auth-code-secret"
    code_verifier = "verifier-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection failed for auth-code-secret with verifier-secret",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthError) as exc_info:
            client.exchange_code_for_token(
                code=auth_code,
                redirect_uri="http://127.0.0.1:12345/cli/callback",
                code_verifier=code_verifier,
            )

    message = str(exc_info.value)
    assert "exchange authorization code for token request failed" in message
    assert "[REDACTED]" in message
    assert auth_code not in message
    assert code_verifier not in message


def test_refresh_access_token_wraps_http_error_and_redacts_refresh_token():
    refresh_token = "refresh-token-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException(
            "timeout while using refresh-token-secret",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthError) as exc_info:
            client.refresh_access_token(refresh_token)

    message = str(exc_info.value)
    assert "refresh access token request failed" in message
    assert "[REDACTED]" in message
    assert refresh_token not in message


def test_exchange_access_token_for_sts_wraps_http_error_and_redacts_access_token():
    access_token = "access-token-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection failed for access-token-secret",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = AliyunOAuthClient(get_oauth_site("CN"), http_client=http_client)
        with pytest.raises(AliyunOAuthError) as exc_info:
            client.exchange_access_token_for_sts(access_token)

    message = str(exc_info.value)
    assert "exchange access token for STS request failed" in message
    assert "[REDACTED]" in message
    assert access_token not in message
