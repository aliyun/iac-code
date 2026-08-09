"""Contract tests for the shared dynamic Alibaba Cloud credential runtime.

Nothing here touches the real metadata service (100.100.100.200), a real STS endpoint
or a real cloud account: the ECS provider is always replaced by an in-process fake.
"""

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from alibabacloud_credentials.provider.refreshable import Credentials
from alibabacloud_credentials.utils import auth_util

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_credentials_runtime import (
    ECS_CREDENTIAL_ERROR_CODES,
    ECS_IMDSV2_REQUIRED,
    ECS_METADATA_DISABLED,
    ECS_METADATA_UNREACHABLE,
    ECS_RAM_ROLE_NOT_FOUND,
    ECS_RAM_ROLE_PROVIDER_NAME,
    ECS_RAM_ROLE_REFRESH_FAILED,
    ECS_RAM_ROLE_RESPONSE_INVALID,
    AliyunCredentialRuntime,
    EcsRamRoleProviderAdapter,
    _create_ecs_provider,
    aliyun_credential_runtime,
    invalidate_aliyun_credential_runtime,
    validate_ecs_credentials,
)

ENV_ROLE_NAME = "ALIBABA_CLOUD_ECS_METADATA"
ENV_METADATA_DISABLED = "ALIBABA_CLOUD_ECS_METADATA_DISABLED"
ENV_IMDSV1_DISABLED = "ALIBABA_CLOUD_IMDSV1_DISABLED"

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolate_ecs_environment(monkeypatch):
    """Never let the developer machine's ECS环境变量 leak into these tests."""
    for name in (ENV_ROLE_NAME, ENV_METADATA_DISABLED, ENV_IMDSV1_DISABLED):
        monkeypatch.delenv(name, raising=False)
    # The SDK snapshots ALIBABA_CLOUD_ECS_METADATA_DISABLED at import time, so clearing
    # os.environ alone does not isolate a test that constructs a real SDK provider: with
    # the variable set when pytest starts, that constructor would refuse to build one.
    monkeypatch.setattr(auth_util, "environment_ecs_metadata_disabled", "false")
    yield


def _fresh_expiration() -> int:
    return int(time.time()) + 3600


_UNSET = object()


class RaisingExpirationCredentials(Credentials):
    """Real credential object whose expiration cannot be read."""

    def __init__(self, *, error: Exception, **kwargs) -> None:
        super().__init__(**kwargs)
        self._error = error

    def get_expiration(self):
        raise self._error


class ExpirationlessCredentials(Credentials):
    """Real credential object without a callable `get_expiration()`, like a plain `ICredentials`."""

    get_expiration = None


class DuckCredentials:
    """Complete duck-typed credential object that is not the SDK's `Credentials`."""

    def get_access_key_id(self):
        return "STS.fake-access-key-id"

    def get_access_key_secret(self):
        return "fake-access-key-secret"

    def get_security_token(self):
        return "fake-security-token"

    def get_provider_name(self):
        return ECS_RAM_ROLE_PROVIDER_NAME

    def get_expiration(self):
        return _fresh_expiration()


def fake_credentials(
    *,
    access_key_id: str = "STS.fake-access-key-id",
    access_key_secret: str = "fake-access-key-secret",
    security_token: str = "fake-security-token",
    expiration=_UNSET,
    expiration_error: Exception | None = None,
    provider_name: str | None = ECS_RAM_ROLE_PROVIDER_NAME,
) -> Credentials:
    """Build the concrete SDK credential object an ECS metadata read hands back.

    Validation requires that exact type, so a duck-typed stand-in could only ever
    exercise the rejection path.
    """
    fields = {
        "access_key_id": access_key_id,
        "access_key_secret": access_key_secret,
        "security_token": security_token,
        "expiration": _fresh_expiration() if expiration is _UNSET else expiration,
        "provider_name": provider_name,
    }
    if expiration_error is not None:
        return RaisingExpirationCredentials(error=expiration_error, **fields)
    return Credentials(**fields)


class FakeProvider:
    """Fake ECS provider that records how it was built and how it is called."""

    def __init__(
        self,
        *,
        credentials=None,
        error: BaseException | None = None,
        cache: bool = False,
    ) -> None:
        self.credentials = credentials if credentials is not None else fake_credentials()
        self.error = error
        self.sync_calls = 0
        self.async_calls = 0
        self.call_threads: list[int] = []
        self._cache = cache
        self._cached = None
        self._lock = threading.Lock()

    def get_credentials(self):
        with self._lock:
            self.call_threads.append(threading.get_ident())
            if self._cache and self._cached is not None:
                return self._cached
            self.sync_calls += 1
            if self.error is not None:
                raise self.error
            self._cached = self.credentials
            return self.credentials

    async def get_credentials_async(self):
        self.async_calls += 1
        raise AssertionError("the raw provider async interface must never be used")

    def get_provider_name(self):
        return ECS_RAM_ROLE_PROVIDER_NAME


class RecordingFactory:
    """Provider factory that records every keyword it was called with."""

    def __init__(self, *, provider_factory=None) -> None:
        self.calls: list[dict] = []
        self.providers: list[FakeProvider] = []
        self._provider_factory = provider_factory or (lambda: FakeProvider())

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        provider = self._provider_factory()
        self.providers.append(provider)
        return provider


def _ecs_credential(role_name: str = "", region_id: str = "cn-hangzhou") -> AliyunCredential:
    return AliyunCredential(mode="EcsRamRole", ram_role_name=role_name, region_id=region_id)


# ── Dependency contract ───────────────────────────────────────────────


def test_pyproject_constrains_the_credential_sdk_versions_the_runtime_needs() -> None:
    """The two capabilities used here are direct dependencies, not lock-file luck."""
    import inspect

    if hasattr(__import__("sys"), "version_info") and __import__("sys").version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - only on 3.10
        import tomli as tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert "alibabacloud-credentials>=1.0.8" in dependencies
    assert "alibabacloud-credentials-api>=1.0.0" in dependencies

    from alibabacloud_credentials.provider.ecs_ram_role import EcsRamRoleCredentialsProvider
    from alibabacloud_credentials_api import ICredentialsProvider

    assert issubclass(EcsRamRoleCredentialsProvider, ICredentialsProvider)
    parameters = inspect.signature(EcsRamRoleCredentialsProvider.__init__).parameters
    assert "async_update_enabled" in parameters
    assert "disable_imds_v1" in parameters
    assert "role_name" in parameters
    # The runtime reads the real expiration from the concrete refreshable credential.
    from alibabacloud_credentials.provider.refreshable import Credentials

    assert callable(Credentials.get_expiration)


def test_adapter_is_accepted_by_the_credentials_sdk_client_and_names_itself() -> None:
    from alibabacloud_credentials.client import Client as CredentialClient

    provider = FakeProvider()
    adapter = EcsRamRoleProviderAdapter(provider, disable_imds_v1=False)
    assert adapter.get_provider_name() == ECS_RAM_ROLE_PROVIDER_NAME

    client = CredentialClient(provider=adapter)
    model = client.get_credential()
    assert model.access_key_id == "STS.fake-access-key-id"
    assert model.security_token == "fake-security-token"
    assert model.type == ECS_RAM_ROLE_PROVIDER_NAME


# ── Role name normalization ───────────────────────────────────────────


@pytest.mark.parametrize("configured", ["", "   ", "\t"])
def test_blank_configured_role_name_reaches_the_factory_as_empty_string(configured, monkeypatch) -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    runtime.ecs_adapter(_ecs_credential(configured))

    assert factory.calls == [{"role_name": "", "disable_imds_v1": False}]
    # `None` would make the SDK fall back to its import-time env snapshot.
    assert factory.calls[0]["role_name"] is not None


@pytest.mark.parametrize("env_value", [None, "", "   ", "\t \n"])
def test_blank_environment_role_name_still_selects_auto_discovery(env_value, monkeypatch) -> None:
    if env_value is not None:
        monkeypatch.setenv(ENV_ROLE_NAME, env_value)
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    runtime.ecs_adapter(_ecs_credential(""))

    # An empty string selects the provider's synchronous role-name discovery instead of
    # requesting a literally blank role name path.
    assert factory.calls == [{"role_name": "", "disable_imds_v1": False}]


def test_explicit_role_name_is_passed_through_and_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ROLE_NAME, "env-role")
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    runtime.ecs_adapter(_ecs_credential("  configured-role  "))

    assert factory.calls == [{"role_name": "configured-role", "disable_imds_v1": False}]


def test_environment_role_name_is_used_when_nothing_is_configured(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ROLE_NAME, " env-role ")
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    runtime.ecs_adapter(_ecs_credential(""))

    assert factory.calls == [{"role_name": "env-role", "disable_imds_v1": False}]


@pytest.mark.parametrize("role_name", ["a/b", "role?x", "role#x", "role\x01name", "role\nname"])
def test_unsafe_role_name_fails_before_any_provider_is_created(role_name) -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    with pytest.raises(ValueError) as excinfo:
        runtime.ecs_adapter(_ecs_credential(role_name))

    assert str(excinfo.value) == ECS_RAM_ROLE_NOT_FOUND
    assert factory.calls == []


# ── Provider construction ────────────────────────────────────────────


def test_provider_is_created_without_background_async_updates() -> None:
    with patch("alibabacloud_credentials.provider.ecs_ram_role.EcsRamRoleCredentialsProvider") as sdk_provider:
        _create_ecs_provider(role_name="fake-role", disable_imds_v1=True)

    assert sdk_provider.call_args.kwargs == {
        "role_name": "fake-role",
        "disable_imds_v1": True,
        # No APScheduler and no SIGINT/SIGTERM handlers in a hosted process.
        "async_update_enabled": False,
    }


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [(None, False), ("", False), ("false", False), ("true", True), (" TRUE ", True)],
)
def test_imdsv1_policy_is_read_from_the_live_environment_per_provider(env_value, expected, monkeypatch) -> None:
    if env_value is not None:
        monkeypatch.setenv(ENV_IMDSV1_DISABLED, env_value)
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    runtime.ecs_adapter(_ecs_credential("fake-role"))

    assert factory.calls[0]["disable_imds_v1"] is expected


def test_imdsv1_policy_change_creates_a_new_provider(monkeypatch) -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    credential = _ecs_credential("fake-role")

    runtime.ecs_adapter(credential)
    monkeypatch.setenv(ENV_IMDSV1_DISABLED, "true")
    runtime.ecs_adapter(credential)

    assert [call["disable_imds_v1"] for call in factory.calls] == [False, True]


def test_metadata_disabled_fails_before_any_provider_is_created(monkeypatch) -> None:
    monkeypatch.setenv(ENV_METADATA_DISABLED, "true")
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    with pytest.raises(ValueError) as excinfo:
        runtime.ecs_adapter(_ecs_credential("fake-role"))

    assert str(excinfo.value) == ECS_METADATA_DISABLED
    assert factory.calls == []


def test_sdk_import_time_disabled_snapshot_maps_to_the_stable_code() -> None:
    """The SDK constructor checks its own import-time env snapshot.

    When that snapshot says "disabled" but the live environment no longer does, the
    runtime still reports the stable code instead of the raw SDK `ValueError`.
    """
    with patch(
        "alibabacloud_credentials.provider.ecs_ram_role.EcsRamRoleCredentialsProvider",
        side_effect=ValueError("IMDS credentials is disabled"),
    ):
        with pytest.raises(ValueError) as excinfo:
            _create_ecs_provider(role_name="fake-role", disable_imds_v1=False)

    assert str(excinfo.value) == ECS_METADATA_DISABLED


# ── Failure classification ───────────────────────────────────────────


class _FakeCredentialError(Exception):
    """Stand-in for the SDK's `CredentialException`, which is a plain `Exception`."""


def _metadata_parse_failure(body: str) -> BaseException:
    """Return the real exception the SDK raises while parsing `body` as a credential response.

    Mirrors `EcsRamRoleCredentialsProvider._refresh_credentials`, which decodes the body,
    reads fields with `dict.get` and feeds `Expiration` straight into `time.strptime`
    without validating either. Using the genuine exception object keeps this test honest
    about the type and chain the classifier actually receives.
    """
    try:
        dic = json.loads(body)
        time.strptime(dic.get("Expiration"), "%Y-%m-%dT%H:%M:%SZ")
    except Exception as error:  # noqa: BLE001 - the exception is the subject under test
        return error
    raise AssertionError("the metadata body parsed successfully, so it cannot drive this test")


def _imds_connection_failure(message: str, *, from_socket_error: bool = False) -> BaseException:
    """Return a real `Tea.exceptions.RetryError`, the type `TeaCore.do_action` raises for IMDS I/O.

    `RetryError` is a plain `Exception` that keeps the socket message in `args` only, so a
    hand-rolled stand-in would not exercise the same classification path. `TeaCore` builds
    it inside `except IOError`, which attaches that failure as implicit context;
    `from_socket_error` reproduces that shape, while the default is the bare error.
    """
    from Tea.exceptions import RetryError

    if not from_socket_error:
        return RetryError(message)
    try:
        try:
            raise TimeoutError(message)
        except TimeoutError:
            raise RetryError(message)
    except RetryError as error:
        return error


@pytest.mark.parametrize(
    ("error", "disable_imds_v1", "expected"),
    [
        (
            _FakeCredentialError("Failed to get RAM session credentials from ECS metadata service. HttpCode=404"),
            False,
            ECS_RAM_ROLE_NOT_FOUND,
        ),
        (
            _FakeCredentialError("Failed to get token from ECS Metadata Service."),
            True,
            ECS_IMDSV2_REQUIRED,
        ),
        (
            # The real SDK message for a refused IMDSv2 token round trip also carries
            # HttpCode=404; that is a required-IMDSv2 failure, not a missing role.
            _FakeCredentialError("Failed to get token from ECS Metadata Service. HttpCode=404"),
            True,
            ECS_IMDSV2_REQUIRED,
        ),
        (
            # A role-credential request that 404s stays a missing-role failure even when
            # IMDSv1 is disabled.
            _FakeCredentialError("Failed to get RAM session credentials from ECS metadata service. HttpCode=404"),
            True,
            ECS_RAM_ROLE_NOT_FOUND,
        ),
        (
            # A bare "token" word must not claim IMDSv2 is required.
            _FakeCredentialError("The SecurityToken is invalid. HttpCode=400"),
            True,
            ECS_RAM_ROLE_REFRESH_FAILED,
        ),
        (json.JSONDecodeError("Expecting value", "not-json", 0), False, ECS_RAM_ROLE_RESPONSE_INVALID),
        (KeyError("AccessKeyId"), False, ECS_RAM_ROLE_RESPONSE_INVALID),
        (
            # `Expiration` missing from an otherwise successful response: `time.strptime`
            # rejects `None` with a TypeError, not a ValueError.
            _metadata_parse_failure('{"Code":"Success","AccessKeyId":"a","AccessKeySecret":"b"}'),
            False,
            ECS_RAM_ROLE_RESPONSE_INVALID,
        ),
        (
            # A non-string `Expiration` fails the same way.
            _metadata_parse_failure('{"Code":"Success","Expiration":1893456000}'),
            False,
            ECS_RAM_ROLE_RESPONSE_INVALID,
        ),
        (
            # Valid JSON whose top level is not an object fails earlier, on `dic.get`.
            _metadata_parse_failure('[{"Code":"Success"}]'),
            False,
            ECS_RAM_ROLE_RESPONSE_INVALID,
        ),
        (
            # A malformed but string `Expiration` still arrives as a ValueError.
            _metadata_parse_failure('{"Code":"Success","Expiration":"not-a-timestamp"}'),
            False,
            ECS_RAM_ROLE_RESPONSE_INVALID,
        ),
        (ConnectionError("connection refused"), False, ECS_METADATA_UNREACHABLE),
        (socket.timeout("timed out"), False, ECS_METADATA_UNREACHABLE),
        (
            # The SDK reports an unreachable IMDS as a Tea `RetryError`, which is neither an
            # OSError nor a ValueError, so it needs its own branch.
            _imds_connection_failure("HTTPConnectionPool(host='100.100.100.200', port=80): connect timed out"),
            False,
            ECS_METADATA_UNREACHABLE,
        ),
        (
            _imds_connection_failure("connect timed out", from_socket_error=True),
            False,
            ECS_METADATA_UNREACHABLE,
        ),
        (
            # A token round trip that explicitly failed keeps the required-IMDSv2 meaning.
            _imds_connection_failure("Failed to get token from ECS Metadata Service. HttpCode=403"),
            True,
            ECS_IMDSV2_REQUIRED,
        ),
        (
            # The role-name and credential requests raise the same type, so a disabled
            # IMDSv1 alone must not relabel an unreachable IMDS as a token failure.
            _imds_connection_failure("connect timed out"),
            True,
            ECS_METADATA_UNREACHABLE,
        ),
        (_FakeCredentialError("unexpected metadata failure"), False, ECS_RAM_ROLE_REFRESH_FAILED),
    ],
)
def test_provider_failures_map_to_the_stable_codes(error, disable_imds_v1, expected) -> None:
    adapter = EcsRamRoleProviderAdapter(FakeProvider(error=error), disable_imds_v1=disable_imds_v1)

    with pytest.raises(ValueError) as excinfo:
        adapter.get_credentials()

    assert str(excinfo.value) == expected
    assert str(excinfo.value) in ECS_CREDENTIAL_ERROR_CODES


def test_failure_codes_never_carry_metadata_response_content() -> None:
    leaked = (
        '{"AccessKeyId":"STS.leaked-id","AccessKeySecret":"leaked-secret",'
        '"SecurityToken":"leaked-token","Code":"Failed"} '
        "http://100.100.100.200/latest/meta-data/ram/security-credentials/leaked-role"
    )
    adapter = EcsRamRoleProviderAdapter(FakeProvider(error=_FakeCredentialError(leaked)), disable_imds_v1=False)

    with pytest.raises(ValueError) as excinfo:
        adapter.get_credentials()

    message = str(excinfo.value)
    assert message in ECS_CREDENTIAL_ERROR_CODES
    for secret in ("leaked-id", "leaked-secret", "leaked-token", "100.100.100.200", "leaked-role"):
        assert secret not in message


def test_stable_codes_from_the_provider_are_not_reclassified() -> None:
    adapter = EcsRamRoleProviderAdapter(FakeProvider(error=ValueError(ECS_METADATA_DISABLED)), disable_imds_v1=False)

    with pytest.raises(ValueError) as excinfo:
        adapter.get_credentials()

    assert str(excinfo.value) == ECS_METADATA_DISABLED


# ── Credential validation ────────────────────────────────────────────


def test_only_the_concrete_refreshable_credentials_type_is_accepted() -> None:
    """A complete duck-typed object still cannot prove it carries a real expiration."""
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(DuckCredentials())

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


@pytest.mark.parametrize("provider_name", ["ram_role_arn", "static_ak", "ECS_RAM_ROLE", "", None])
def test_credentials_from_another_provider_are_rejected(provider_name) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(provider_name=provider_name))

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


def test_instance_metadata_credentials_are_accepted() -> None:
    credentials = fake_credentials()

    assert validate_ecs_credentials(credentials) is credentials
    assert credentials.get_provider_name() == ECS_RAM_ROLE_PROVIDER_NAME


def test_the_sdk_import_time_snapshot_is_isolated_from_the_local_environment() -> None:
    """Proof that constructing a real SDK provider below cannot depend on the local env."""
    assert os.environ.get(ENV_METADATA_DISABLED) is None
    assert auth_util.environment_ecs_metadata_disabled == "false"


def test_the_sdk_provider_stamps_the_validated_provider_name() -> None:
    """The exact name the validation demands is the one the real SDK provider writes."""
    from alibabacloud_credentials.provider.ecs_ram_role import EcsRamRoleCredentialsProvider

    provider = EcsRamRoleCredentialsProvider(
        role_name="fake-role",
        disable_imds_v1=False,
        async_update_enabled=False,
    )

    assert provider.get_provider_name() == ECS_RAM_ROLE_PROVIDER_NAME


@pytest.mark.parametrize(
    "kwargs",
    [
        {"access_key_id": ""},
        {"access_key_secret": ""},
        {"security_token": ""},
    ],
)
def test_missing_credential_fields_fail(kwargs) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(**kwargs))

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


def test_credentials_without_a_concrete_expiration_accessor_fail() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(ExpirationlessCredentials())

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


@pytest.mark.parametrize("expiration", ["2099-01-01T00:00:00Z", None, 1.5, True])
def test_non_integer_expiration_fails(expiration) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(expiration=expiration))

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


def test_unreadable_expiration_fails() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(expiration_error=RuntimeError("boom")))

    assert str(excinfo.value) == ECS_RAM_ROLE_RESPONSE_INVALID


def test_expired_credentials_fail_instead_of_being_signed() -> None:
    now = int(time.time())
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(expiration=now - 1), now=now)

    assert str(excinfo.value) == ECS_RAM_ROLE_REFRESH_FAILED


def test_credentials_inside_the_clock_skew_window_fail() -> None:
    now = int(time.time())
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(fake_credentials(expiration=now + 30), now=now)

    assert str(excinfo.value) == ECS_RAM_ROLE_REFRESH_FAILED


def test_refresh_failure_only_serves_a_still_valid_cache() -> None:
    """`StaleValueBehavior.ALLOW` hands back the previous value when a refresh fails."""
    now = int(time.time())
    still_valid = fake_credentials(expiration=now + 3600)
    assert validate_ecs_credentials(still_valid, now=now) is still_valid

    stale = fake_credentials(expiration=now + 5)
    with pytest.raises(ValueError) as excinfo:
        validate_ecs_credentials(stale, now=now)
    assert str(excinfo.value) == ECS_RAM_ROLE_REFRESH_FAILED


def test_sdk_client_channel_also_rejects_expired_credentials() -> None:
    runtime = AliyunCredentialRuntime(
        provider_factory=RecordingFactory(
            provider_factory=lambda: FakeProvider(credentials=fake_credentials(expiration=int(time.time()) - 10))
        )
    )
    client = runtime.sdk_client(_ecs_credential("fake-role"))

    with pytest.raises(ValueError) as excinfo:
        client.get_credential()

    assert str(excinfo.value) == ECS_RAM_ROLE_REFRESH_FAILED


# ── Provider cache ───────────────────────────────────────────────────


def test_same_configuration_reuses_one_provider() -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    credential = _ecs_credential("fake-role")

    first = runtime.ecs_adapter(credential)
    second = runtime.ecs_adapter(_ecs_credential("fake-role"))

    assert first is second
    assert len(factory.calls) == 1


def test_role_name_change_invalidates_the_cache() -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    first = runtime.ecs_adapter(_ecs_credential("role-a"))
    second = runtime.ecs_adapter(_ecs_credential("role-b"))

    assert first is not second
    assert [call["role_name"] for call in factory.calls] == ["role-a", "role-b"]


def test_invalidate_drops_cached_providers() -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    credential = _ecs_credential("fake-role")

    first = runtime.ecs_adapter(credential)
    runtime.invalidate()
    second = runtime.ecs_adapter(credential)

    assert first is not second
    assert len(factory.calls) == 2


def test_saving_credentials_invalidates_the_process_runtime(monkeypatch, tmp_path) -> None:
    from iac_code.services.providers import aliyun as aliyun_module

    monkeypatch.setattr(
        aliyun_module,
        "get_cloud_credentials_path",
        lambda: tmp_path / ".cloud-credentials.yml",
    )
    factory = RecordingFactory()
    runtime = aliyun_credential_runtime()
    monkeypatch.setattr(runtime, "_provider_factory", factory)
    runtime.invalidate()
    credential = _ecs_credential("fake-role")
    first = runtime.ecs_adapter(credential)

    aliyun_module.AliyunCredentials.save(credential)

    assert runtime.ecs_adapter(credential) is not first
    invalidate_aliyun_credential_runtime()


def test_cached_providers_stay_bounded() -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    for index in range(12):
        runtime.ecs_adapter(_ecs_credential("role-{}".format(index)))

    assert len(runtime._adapters) == 8


# ── Async behaviour ──────────────────────────────────────────────────


def test_resolve_runs_the_blocking_provider_call_off_the_event_loop() -> None:
    provider = FakeProvider()
    factory = RecordingFactory(provider_factory=lambda: provider)
    runtime = AliyunCredentialRuntime(provider_factory=factory)

    async def scenario():
        loop_thread = threading.get_ident()
        resolved = await runtime.resolve(_ecs_credential("fake-role", region_id="cn-shanghai"))
        return loop_thread, resolved

    loop_thread, resolved = asyncio.run(scenario())

    assert provider.call_threads and all(ident != loop_thread for ident in provider.call_threads)
    assert provider.async_calls == 0
    assert resolved.mode == "StsToken"
    assert resolved.access_key_id == "STS.fake-access-key-id"
    assert resolved.access_key_secret == "fake-access-key-secret"
    assert resolved.sts_token == "fake-security-token"
    # The requested region must survive the credential swap.
    assert resolved.region_id == "cn-shanghai"


def test_sdk_client_async_path_uses_the_synchronous_adapter_in_a_worker_thread() -> None:
    provider = FakeProvider()
    factory = RecordingFactory(provider_factory=lambda: provider)
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    client = runtime.sdk_client(_ecs_credential("fake-role"))

    async def scenario():
        loop_thread = threading.get_ident()
        model = await client.get_credential_async()
        return loop_thread, model

    loop_thread, model = asyncio.run(scenario())

    assert model.access_key_id == "STS.fake-access-key-id"
    # The raw provider's async interface would run the SDK lock and IMDS I/O on the loop
    # and would not auto-discover the role name for an empty string.
    assert provider.async_calls == 0
    assert provider.call_threads and all(ident != loop_thread for ident in provider.call_threads)


def test_concurrent_resolution_creates_one_provider_and_one_metadata_fetch() -> None:
    provider = FakeProvider(cache=True)
    factory = RecordingFactory(provider_factory=lambda: provider)
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    credential = _ecs_credential("fake-role")

    async def scenario():
        return await asyncio.gather(*(runtime.resolve(credential) for _ in range(20)))

    resolved = asyncio.run(scenario())

    assert len(resolved) == 20
    assert len(factory.calls) == 1
    # Steady-state concurrency must not multiply into one IMDS round trip per caller;
    # the SDK's single-flight refresh may still allow a couple of duplicates.
    assert provider.sync_calls <= 2


# ── Non-ECS modes ────────────────────────────────────────────────────


def test_sdk_client_returns_none_for_static_modes() -> None:
    runtime = AliyunCredentialRuntime(provider_factory=RecordingFactory())

    assert runtime.sdk_client(None) is None
    assert runtime.sdk_client(AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake")) is None
    assert runtime.sdk_client(AliyunCredential(mode="StsToken", sts_token="fake")) is None


def test_resolve_returns_static_credentials_untouched() -> None:
    factory = RecordingFactory()
    runtime = AliyunCredentialRuntime(provider_factory=factory)
    credential = AliyunCredential(mode="AK", access_key_id="fake-id", access_key_secret="fake-secret")

    resolved = asyncio.run(runtime.resolve(credential))

    assert resolved is credential
    assert factory.calls == []
