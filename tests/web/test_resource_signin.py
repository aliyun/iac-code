from __future__ import annotations

from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_identity import AliyunCallerIdentity
from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context
from iac_code.web.resource_signin import (
    _SITES,
    ResourceSigninError,
    ResourceSigninSite,
    _blocking_signin_token,
    clean_signin_request,
    create_resource_console_signin,
    resource_console_hosts,
    resource_console_open_capability,
    validate_console_destination,
)

CN_STACK = "https://ros.console.aliyun.com/cn-hangzhou/stacks/stack-1"
INTL_STACK = "https://ros-intl.console.alibabacloud.com/cn-hangzhou/stacks/stack-1"
CN_EMBEDDED_STACK = "https://ros4service.console.aliyun.com/cn-hangzhou/stacks/stack-1"
INTL_EMBEDDED_STACK = "https://ros4service.console.alibabacloud.com/cn-hangzhou/stacks/stack-1"


@pytest.fixture(autouse=True)
def _no_configured_console_role(monkeypatch):
    monkeypatch.setattr(
        "iac_code.web.settings.console_signin_role_settings",
        lambda: {"roleArn": "", "configured": False},
    )


async def role_identity(_credential, _region_id):
    return AliyunCallerIdentity(kind="ram_role", account_id="1001", subject_id="role-1")


async def account_identity(_credential, _region_id):
    return AliyunCallerIdentity(kind="account", account_id="1001", subject_id="1001")


def request_for(credential, *, preferred="aliyun", destinations=None):
    return {
        "accountScope": credential_reference_context(credential)["account_scope"],
        "preferredPartition": preferred,
        "destinations": destinations or {"aliyun": CN_STACK, "alibabacloud": INTL_STACK},
    }


def test_console_destination_allowlist_comes_from_catalog_and_rejects_url_confusion():
    hosts = resource_console_hosts()
    assert "ros.console.aliyun.com" in hosts["aliyun"]
    assert "ros-intl.console.alibabacloud.com" in hosts["alibabacloud"]
    assert "ros4service.console.aliyun.com" in hosts["aliyun"]
    assert "ros4service.console.alibabacloud.com" in hosts["alibabacloud"]
    assert "ecs.console.aliyun.com" in hosts["aliyun"]
    assert "ecs4service.console.aliyun.com" in hosts["aliyun"]
    assert "ram4service.console.aliyun.com" in hosts["aliyun"]
    assert "vpc4service.console.aliyun.com" in hosts["aliyun"]
    assert "ads4service.console.aliyun.com" in hosts["aliyun"]
    assert "slb4service.console.aliyun.com" in hosts["aliyun"]
    assert "cloudmonitor4service.console.aliyun.com" in hosts["aliyun"]
    assert "dnsnext4service.console.aliyun.com" in hosts["aliyun"]
    assert "expressconnect.console.aliyun.com" in hosts["aliyun"]
    assert "expressconnect4service.console.aliyun.com" in hosts["aliyun"]
    assert "apigateway.console.aliyun.com" in hosts["aliyun"]
    assert "apigateway4service.console.aliyun.com" not in hosts["aliyun"]
    assert validate_console_destination(CN_STACK, "aliyun") == CN_STACK
    assert validate_console_destination(CN_EMBEDDED_STACK, "aliyun") == CN_EMBEDDED_STACK
    assert validate_console_destination(INTL_EMBEDDED_STACK, "alibabacloud") == INTL_EMBEDDED_STACK
    assert (
        validate_console_destination(
            "https://ecs4service.console.aliyun.com/#/server/i-fixture/detail?regionId=cn-hangzhou", "aliyun"
        )
        == "https://ecs4service.console.aliyun.com/#/server/i-fixture/detail?regionId=cn-hangzhou"
    )
    assert (
        validate_console_destination(
            "https://elasticsearch-cn-hangzhou.console.aliyun.com/#/instances/es-1/base", "aliyun"
        )
        == "https://elasticsearch-cn-hangzhou.console.aliyun.com/#/instances/es-1/base"
    )
    assert (
        validate_console_destination(
            "https://ads4service.console.aliyun.com/adb/cn-hangzhou/instances/v3/amv-fixture/basic", "aliyun"
        )
        == "https://ads4service.console.aliyun.com/adb/cn-hangzhou/instances/v3/amv-fixture/basic"
    )
    assert (
        validate_console_destination(
            "https://expressconnect4service.console.aliyun.com/peerconnection/cn-wulanchabu/vbr2vpc"
            "?ConnectionType=vbr2vpc&ProductForm=expressconnect&TabKey=vbr2vpc",
            "aliyun",
        )
        == "https://expressconnect4service.console.aliyun.com/peerconnection/cn-wulanchabu/vbr2vpc"
        "?ConnectionType=vbr2vpc&ProductForm=expressconnect&TabKey=vbr2vpc"
    )
    assert (
        validate_console_destination(
            "https://elasticsearch4service-cn-hangzhou.console.aliyun.com/#/instances/es-1/base", "aliyun"
        )
        == "https://elasticsearch4service-cn-hangzhou.console.aliyun.com/#/instances/es-1/base"
    )
    for value in (
        "http://ros.console.aliyun.com/cn-hangzhou/stacks/stack-1",
        "https://ros.console.aliyun.com.evil.test/cn-hangzhou/stacks/stack-1",
        "https://user@ros.console.aliyun.com/cn-hangzhou/stacks/stack-1",
        "https://ros.console.aliyun.com:443/cn-hangzhou/stacks/stack-1",
        "https://elasticsearch-cn-hangzhou.evil.test/#/instances/es-1/base",
        "https://elasticsearch-CN_HANGZHOU.console.aliyun.com/#/instances/es-1/base",
        "https://apigateway4service.console.aliyun.com/#/cn-hangzhou/groups",
        "https://ros.console.aliyun.com\n.evil.test/",
    ):
        with pytest.raises(ResourceSigninError, match="resource_signin_destination_invalid"):
            validate_console_destination(value, "aliyun")


def test_signin_request_requires_a_matching_preferred_allowlisted_destination():
    credential = AliyunCredential(mode="StsToken", access_key_id="fake", access_key_secret="fake", sts_token="fake")
    account, destinations = clean_signin_request(request_for(credential))
    assert account.startswith("credential:")
    assert list(destinations) == ["aliyun", "alibabacloud"]
    with pytest.raises(ResourceSigninError, match="resource_signin_request_invalid"):
        clean_signin_request({**request_for(credential), "extra": True})
    with pytest.raises(ResourceSigninError, match="resource_signin_request_invalid"):
        clean_signin_request(
            {**request_for(credential), "preferredPartition": "alibabacloud", "destinations": {"aliyun": CN_STACK}}
        )


@pytest.mark.asyncio
async def test_open_capability_defaults_to_external_and_accepts_explicit_role_modes():
    assert await resource_console_open_capability(credential_loader=lambda: None) == {"openMode": "external"}
    for mode in ("AK", "OAuth"):
        credential = AliyunCredential(mode=mode, access_key_id="fake", access_key_secret="fake")
        assert await resource_console_open_capability(credential_loader=lambda credential=credential: credential) == {
            "openMode": "external"
        }
    for mode in ("RamRoleArn", "EcsRamRole"):
        credential = AliyunCredential(mode=mode)
        assert await resource_console_open_capability(credential_loader=lambda credential=credential: credential) == {
            "openMode": "embedded"
        }


@pytest.mark.asyncio
async def test_open_capability_verifies_whether_a_generic_sts_token_is_role_backed():
    credential = AliyunCredential(mode="StsToken", access_key_id="fake", access_key_secret="fake", sts_token="fake")
    assert await resource_console_open_capability(
        credential_loader=lambda: credential, identity_resolver=role_identity
    ) == {"openMode": "embedded"}
    assert await resource_console_open_capability(
        credential_loader=lambda: credential, identity_resolver=account_identity
    ) == {"openMode": "external"}

    async def rejected_identity(_credential, _region_id):
        raise RuntimeError("offline")

    assert await resource_console_open_capability(
        credential_loader=lambda: credential, identity_resolver=rejected_identity
    ) == {"openMode": "external"}


@pytest.mark.asyncio
async def test_configured_console_role_enables_embedded_mode_for_any_assumable_source(monkeypatch):
    source = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")
    assumed = AliyunCredential(
        mode="StsToken",
        access_key_id="sts-id",
        access_key_secret="sts-secret",
        sts_token="sts-token",
        sts_expiration=4_000_000_000,
    )
    monkeypatch.setattr(
        "iac_code.web.settings.console_signin_role_settings",
        lambda: {"roleArn": "acs:ram::1001:role/PreviewRole", "configured": True},
    )

    async def resolve(credential, role_arn):
        assert credential is source
        assert role_arn == "acs:ram::1001:role/PreviewRole"
        return assumed

    monkeypatch.setattr("iac_code.web.resource_signin_role.console_signin_role_credential", resolve)
    assert await resource_console_open_capability(credential_loader=lambda: source) == {"openMode": "embedded"}


@pytest.mark.asyncio
async def test_sts_credential_returns_only_single_use_login_url():
    credential = AliyunCredential(
        mode="StsToken",
        access_key_id="fake-temporary-id",
        access_key_secret="fake-temporary-secret",
        sts_token="fake-security-token",
    )
    calls = []

    async def token(site, selected):
        calls.append((site.partition, selected))
        return "fake-signin-token"

    result = await create_resource_console_signin(
        request_for(credential),
        credential_loader=lambda: credential,
        token_fetcher=token,
        identity_resolver=role_identity,
    )
    assert [partition for partition, _ in calls] == ["aliyun"]
    assert calls[0][1] is credential
    assert result["status"] == "ready"
    assert result["destination"] == CN_STACK
    parsed = urlsplit(result["url"])
    query = parse_qs(parsed.query)
    assert (parsed.scheme, parsed.hostname, parsed.path) == ("https", "signin.aliyun.com", "/federation")
    assert query == {
        "Action": ["Login"],
        "Destination": [CN_STACK],
        "LoginUrl": ["https://signin.aliyun.com/login.htm"],
        "SigninToken": ["fake-signin-token"],
    }
    assert all(
        secret not in result["url"] for secret in ("fake-temporary-id", "fake-temporary-secret", "fake-security-token")
    )


@pytest.mark.asyncio
async def test_non_oauth_credentials_probe_the_other_site_after_rejection():
    credential = AliyunCredential(mode="StsToken", access_key_id="fake", access_key_secret="fake", sts_token="fake")
    calls = []

    async def token(site, _selected):
        calls.append(site.partition)
        if site.partition == "aliyun":
            raise ResourceSigninError("resource_signin_credentials_rejected")
        return "fake-intl-signin-token"

    result = await create_resource_console_signin(
        request_for(credential),
        credential_loader=lambda: credential,
        token_fetcher=token,
        identity_resolver=role_identity,
    )
    assert calls == ["aliyun", "alibabacloud"]
    assert result["consolePartition"] == "alibabacloud"
    assert result["destination"] == INTL_STACK
    assert urlsplit(result["url"]).hostname == "signin.alibabacloud.com"


@pytest.mark.asyncio
async def test_oauth_site_is_authoritative_and_does_not_probe_another_partition():
    credential = AliyunCredential(
        mode="OAuth",
        access_key_id="fake",
        access_key_secret="fake",
        sts_token="fake",
        sts_expiration=4_000_000_000,
        oauth_site_type="INTL",
        oauth_refresh_token="fake-refresh",
    )
    calls = []

    async def token(site, _selected):
        calls.append(site.partition)
        return "fake-token"

    result = await create_resource_console_signin(
        request_for(credential),
        credential_loader=lambda: credential,
        token_fetcher=token,
        identity_resolver=role_identity,
    )
    assert calls == ["alibabacloud"]
    assert result["destination"] == INTL_STACK


@pytest.mark.asyncio
async def test_account_oauth_identity_cannot_be_used_as_an_assumed_role_session():
    credential = AliyunCredential(
        mode="OAuth",
        access_key_id="fake",
        access_key_secret="fake",
        sts_token="fake",
        sts_expiration=4_000_000_000,
        oauth_site_type="CN",
    )
    called = False

    async def account_identity(_credential, _region_id):
        return AliyunCallerIdentity(kind="account", account_id="1001", subject_id="1001")

    async def token(_site, _selected):
        nonlocal called
        called = True
        return "fake-token"

    with pytest.raises(ResourceSigninError, match="resource_signin_role_required"):
        await create_resource_console_signin(
            request_for(credential),
            credential_loader=lambda: credential,
            token_fetcher=token,
            identity_resolver=account_identity,
        )
    assert called is False


@pytest.mark.asyncio
async def test_plain_ak_requires_role_backed_temporary_credentials():
    credential = AliyunCredential(mode="AK", access_key_id="fake-id", access_key_secret="fake-secret")
    with pytest.raises(ResourceSigninError, match="resource_signin_temporary_credentials_required"):
        await create_resource_console_signin(request_for(credential), credential_loader=lambda: credential)


@pytest.mark.asyncio
async def test_changed_credential_scope_is_rejected_before_network():
    expected = AliyunCredential(mode="StsToken", access_key_id="expected", access_key_secret="fake", sts_token="fake")
    current = AliyunCredential(mode="StsToken", access_key_id="current", access_key_secret="fake", sts_token="fake")
    called = False

    async def token(_site, _selected):
        nonlocal called
        called = True
        return "fake-token"

    with pytest.raises(ResourceSigninError, match="resource_signin_account_changed"):
        await create_resource_console_signin(
            request_for(expected),
            credential_loader=lambda: current,
            token_fetcher=token,
            identity_resolver=role_identity,
        )
    assert called is False


@pytest.mark.asyncio
async def test_session_validated_scope_from_a_prior_process_can_be_used():
    current = AliyunCredential(mode="StsToken", access_key_id="current", access_key_secret="fake", sts_token="fake")

    async def token(_site, _selected):
        return "fake-token"

    payload = {
        **request_for(current),
        "accountScope": "credential:prior-process",
    }
    result = await create_resource_console_signin(
        payload,
        credential_loader=lambda: current,
        token_fetcher=token,
        identity_resolver=role_identity,
        allow_unknown_account_scope=True,
    )
    assert result["status"] == "ready"


@pytest.mark.asyncio
async def test_ram_role_arn_is_resolved_before_signin(monkeypatch):
    source = AliyunCredential(
        mode="RamRoleArn",
        access_key_id="fake-source-id",
        access_key_secret="fake-source-secret",
        ram_role_arn="acs:ram::1001:role/FakeRole",
    )
    temporary = AliyunCredential(
        mode="StsToken", access_key_id="fake-sts-id", access_key_secret="fake-sts-secret", sts_token="fake-sts-token"
    )

    class Runtime:
        async def resolve(self, credential):
            assert credential is source
            return temporary

    monkeypatch.setattr(
        "iac_code.services.providers.aliyun_credentials_runtime.aliyun_credential_runtime", lambda: Runtime()
    )

    async def token(_site, selected):
        assert selected is temporary
        return "fake-token"

    result = await create_resource_console_signin(
        request_for(source),
        credential_loader=lambda: source,
        token_fetcher=token,
        identity_resolver=role_identity,
    )
    assert result["status"] == "ready"


@pytest.mark.parametrize("ticket_type", ["normal", "mini"])
def test_token_exchange_never_exposes_a_rejected_secret(monkeypatch, ticket_type):
    credential = AliyunCredential(
        mode="StsToken",
        access_key_id="fake-id",
        access_key_secret="do-not-expose-this-secret",
        sts_token="fake-token",
    )

    class Opener:
        def open(self, request, timeout):
            assert timeout == 10 and "do-not-expose-this-secret" in request.full_url
            assert parse_qs(urlsplit(request.full_url).query)["TicketType"] == [ticket_type]
            raise URLError("offline")

    monkeypatch.setattr("iac_code.web.resource_signin.build_opener", lambda *_args: Opener())
    with pytest.raises(ResourceSigninError) as raised:
        _blocking_signin_token(ResourceSigninSite("aliyun", _SITES["aliyun"].origin, ticket_type), credential)
    assert str(raised.value) == "resource_signin_credentials_rejected"
    assert "do-not-expose-this-secret" not in str(raised.value)


def test_http_endpoint_is_session_scoped_and_never_cacheable(tmp_path, monkeypatch):
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="signin")

    async def signin(payload, *, allow_unknown_account_scope, login_url=None):
        assert payload == {"fixture": True, "accountScope": "fixture-scope"}
        assert allow_unknown_account_scope is True
        assert login_url is None
        return {
            "status": "ready",
            "consolePartition": "aliyun",
            "url": "https://signin.aliyun.com/federation?SigninToken=do-not-return-to-javascript",
            "destination": CN_STACK,
        }

    async def capability():
        return {"openMode": "embedded"}

    monkeypatch.setattr("iac_code.web.resource_signin.create_resource_console_signin", signin)
    monkeypatch.setattr("iac_code.web.resource_signin.resource_console_open_capability", capability)
    monkeypatch.setattr(
        "iac_code.web.resource_accounts.load_configured_resource_context",
        lambda: {"account_scope": "fixture-scope", "console_partition": "aliyun"},
    )
    with TestClient(create_app(session_manager=manager)) as client:
        prefix = "/api/sessions/{}/resource-references/signin".format(session.web_session_id)
        response = client.post(prefix, json={"fixture": True, "accountScope": "fixture-scope"})
        assert response.status_code == 200
        relay_path = response.json()["url"]
        assert relay_path.startswith(prefix + "-relay/")
        assert "SigninToken" not in relay_path
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"
        capability_response = client.get(prefix + "-capability")
        assert capability_response.status_code == 200
        assert capability_response.json() == {"openMode": "embedded"}
        assert capability_response.headers["cache-control"] == "no-store"
        relay = client.get(relay_path, follow_redirects=False)
        assert relay.status_code == 302
        assert relay.headers["location"].startswith("https://signin.aliyun.com/federation?")
        assert relay.headers["cache-control"] == "no-store"
        assert relay.headers["referrer-policy"] == "no-referrer"
        assert client.get(relay_path, follow_redirects=False).status_code == 404
        assert client.post(prefix, content=b"not-json").status_code == 400
        assert client.post("/api/sessions/missing/resource-references/signin", json={}).status_code == 404
        assert client.get("/api/sessions/missing/resource-references/signin-capability").status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "partition, destination, ticket_type",
    [
        ("aliyun", "https://ecs.console.aliyun.com/#/securityGroupDetail/region/cn-hangzhou/groupId/sg-test", "normal"),
        ("aliyun", CN_STACK, "normal"),
        ("aliyun", "https://ecs.console.aliyun.com/#/4service", "normal"),
        (
            "aliyun",
            "https://ecs4service.console.aliyun.com/securityGroupDetail/region/cn-hangzhou/groupId/sg-test",
            "mini",
        ),
        ("aliyun", CN_EMBEDDED_STACK, "mini"),
        ("aliyun", "https://elasticsearch-cn-hangzhou.console.aliyun.com/#/instances/es-test/base", "normal"),
        ("aliyun", "https://elasticsearch4service-cn-hangzhou.console.aliyun.com/#/instances/es-test/base", "mini"),
        ("alibabacloud", INTL_STACK, "normal"),
        ("alibabacloud", INTL_EMBEDDED_STACK, "mini"),
    ],
)
async def test_signin_ticket_tracks_validated_destination_not_popup_mode(partition, destination, ticket_type):
    credential = AliyunCredential(mode="StsToken", access_key_id="fake", access_key_secret="fake", sts_token="fake")
    fetched = []

    async def token(site, selected):
        assert selected is credential
        fetched.append(site)
        assert site.partition == partition
        assert site.ticket_type == ticket_type
        return "fake-token"

    result = await create_resource_console_signin(
        request_for(credential, preferred=partition, destinations={partition: destination}),
        credential_loader=lambda: credential,
        identity_resolver=role_identity,
        token_fetcher=token,
    )
    assert len(fetched) == 1
    assert result["destination"] == destination
    query = parse_qs(urlsplit(result["url"]).query)
    assert query["Destination"] == [destination]
    assert query["LoginUrl"] == [_SITES[partition].login_url]
    assert _SITES[partition].ticket_type == "normal"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("base_url", ["http://127.0.0.1:16100", "https://127.0.0.1:16101"])
def test_encrypted_signin_endpoint_never_uses_consumable_relay_for_recovery(tmp_path, monkeypatch, stream, base_url):
    import json

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager
    from iac_code.web.token_transport import TokenTransport
    from tests.web.test_token_transport import BrowserProtocol, _decode, _encode

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="signin")
    login_urls = []

    async def signin(payload, *, allow_unknown_account_scope, login_url=None):
        assert payload["accountScope"] == "fixture-scope"
        assert allow_unknown_account_scope is True
        login_urls.append(login_url)
        return {
            "status": "ready",
            "consolePartition": "aliyun",
            "url": "https://signin.aliyun.com/federation?SigninToken=fake-token",
            "destination": CN_STACK,
        }

    monkeypatch.setattr("iac_code.web.resource_signin.create_resource_console_signin", signin)
    monkeypatch.setattr(
        "iac_code.web.resource_accounts.load_configured_resource_context",
        lambda: {"account_scope": "fixture-scope", "console_partition": "aliyun"},
    )
    token = _encode(bytes(range(32)))
    with TestClient(TokenTransport(create_app(session_manager=manager), token), base_url=base_url) as client:
        browser = BrowserProtocol(client, token)
        prefix = "/api/sessions/{}/resource-references/signin".format(session.web_session_id)
        request = {
            "method": "POST",
            "path": prefix,
            "headers": {"content-type": "application/json", "host": "attacker.example"},
            "body": _encode(json.dumps({"accountScope": "fixture-scope"}).encode()),
        }
        message_type = "stream" if stream else "request"
        response = client.post(
            "/api/token/" + message_type,
            json=browser.encrypt(message_type, 1, json.dumps(request).encode()),
            headers={"host": "198.51.100.1:9999", "x-forwarded-host": "attacker.example"},
        )
        assert response.status_code == 200
        if stream:
            envelopes = [json.loads(line) for line in response.iter_lines() if line]
            start = json.loads(browser.decrypt(envelopes[0], "stream-start"))
            assert start["status"] == 200
            body = b"".join(
                _decode(json.loads(browser.decrypt(envelope, "stream-body"))["body"])
                for envelope in envelopes
                if envelope["type"] == "stream-body"
            )
        else:
            payload = json.loads(browser.decrypt(response.json(), "response"))
            assert payload["status"] == 200
            body = _decode(payload["body"])
        result = json.loads(body)
        assert login_urls == [None]
        assert result["url"].startswith(prefix + "-relay/")
        assert "SigninToken" not in body.decode()
