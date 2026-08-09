"""Fixtures shared by the Alibaba Cloud tool tests."""

from __future__ import annotations

import pytest

from tests.tools.cloud.aliyun._ecs_ram_role_fakes import (
    ECS_ENVIRONMENT_VARIABLES,
    FakeEcsProvider,
    FakeEcsRuntime,
)


@pytest.fixture
def fake_ecs_runtime(monkeypatch: pytest.MonkeyPatch) -> FakeEcsRuntime:
    """Install a process runtime backed by a fake ECS metadata provider."""
    from iac_code.services.providers import aliyun_credentials_runtime as runtime_module

    for name in ECS_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    handle = FakeEcsRuntime(runtime=None)

    def factory(*, role_name: str, disable_imds_v1: bool) -> FakeEcsProvider:
        provider = FakeEcsProvider(role_name=role_name, disable_imds_v1=disable_imds_v1, error=handle.error)
        handle.providers.append(provider)
        return provider

    handle.runtime = runtime_module.AliyunCredentialRuntime(provider_factory=factory)
    monkeypatch.setattr(runtime_module, "_runtime", handle.runtime, raising=False)
    return handle
