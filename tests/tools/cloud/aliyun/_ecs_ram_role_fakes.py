"""Shared in-process fakes for ECS instance RAM role credential transport tests.

Every test that exercises the `EcsRamRole` signing path must run without touching the
real instance metadata service, so the shared credential runtime is pointed at a fake
provider that records which thread each metadata read happened on.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from alibabacloud_credentials.provider.refreshable import Credentials

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_credentials_runtime import ECS_RAM_ROLE_PROVIDER_NAME

ECS_ENVIRONMENT_VARIABLES = (
    "ALIBABA_CLOUD_ECS_METADATA",
    "ALIBABA_CLOUD_ECS_METADATA_DISABLED",
    "ALIBABA_CLOUD_IMDSV1_DISABLED",
)


def fake_ecs_credentials() -> Credentials:
    """Build the concrete SDK credential object an instance metadata read hands back.

    The runtime validates that exact type and provider name, so the fake cannot be a
    duck-typed stand-in.
    """
    return Credentials(
        access_key_id="STS.fake-ecs-ak",
        access_key_secret="fake-ecs-secret",
        security_token="fake-ecs-sts",
        expiration=int(time.time()) + 3600,
        provider_name=ECS_RAM_ROLE_PROVIDER_NAME,
    )


class FakeEcsProvider:
    """Fake `EcsRamRoleCredentialsProvider`; async use is a hard failure."""

    def __init__(self, *, role_name: str, disable_imds_v1: bool, error: BaseException | None = None) -> None:
        self.role_name = role_name
        self.disable_imds_v1 = disable_imds_v1
        self.error = error
        self.call_threads: list[int] = []
        self.async_calls = 0

    def get_credentials(self) -> Credentials:
        self.call_threads.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return fake_ecs_credentials()

    async def get_credentials_async(self) -> Credentials:
        self.async_calls += 1
        raise AssertionError("the raw ECS provider async interface must never be used")

    def get_provider_name(self) -> str:
        return ECS_RAM_ROLE_PROVIDER_NAME


@dataclass
class FakeEcsRuntime:
    """Handle onto the installed runtime and the providers it created."""

    runtime: Any
    providers: list[FakeEcsProvider] = field(default_factory=list)
    error: BaseException | None = None

    def credential(self, role_name: str = "fake-ecs-role", region_id: str = "cn-shanghai") -> AliyunCredential:
        return AliyunCredential(mode="EcsRamRole", ram_role_name=role_name, region_id=region_id)

    def fail_with(self, error: BaseException) -> None:
        """Make every provider created from now on raise `error` from metadata reads."""
        self.error = error
