"""Tests for AttributeBuilder."""

import pytest

from iac_code.services.telemetry.attributes import AttributeBuilder
from iac_code.services.telemetry.identity import Identity


@pytest.fixture
def identity(tmp_path):
    return Identity(tmp_path / "settings.yml")


@pytest.fixture
def builder(identity):
    return AttributeBuilder(identity, service_name="iac-code", service_version="0.1.0")


def test_resource_contains_service_name_and_version(builder):
    attrs = builder.build_resource()
    assert attrs["service.name"] == "iac-code"
    assert attrs["service.version"] == "0.1.0"


def test_resource_contains_os_type_and_arch(builder):
    attrs = builder.build_resource()
    assert attrs["os.type"] in ("linux", "darwin", "win32")
    assert isinstance(attrs["host.arch"], str) and attrs["host.arch"]


def test_resource_contains_user_id(builder):
    assert builder.build_resource()["user.id"].startswith("iac_user_")


def test_resource_contains_session_id(builder):
    assert builder.build_resource()["session.id"].startswith("iac_sess_")


def test_resource_contains_tenant_id_when_set(builder, monkeypatch):
    monkeypatch.setenv("IAC_CODE_TENANT_ID", "acme")
    assert builder.build_resource()["tenant.id"] == "iac_tenant_acme"


def test_resource_omits_tenant_id_when_unset(builder, monkeypatch):
    monkeypatch.delenv("IAC_CODE_TENANT_ID", raising=False)
    assert "tenant.id" not in builder.build_resource()


def test_resource_contains_host_name(builder):
    attrs = builder.build_resource()
    assert isinstance(attrs["host.name"], str) and attrs["host.name"]


def test_resource_default_deployment_environment_is_production(builder, monkeypatch):
    monkeypatch.delenv("IAC_CODE_ENV", raising=False)
    assert builder.build_resource()["deployment.environment"] == "production"


def test_resource_deployment_environment_overridable(builder, monkeypatch):
    monkeypatch.setenv("IAC_CODE_ENV", "staging")
    assert builder.build_resource()["deployment.environment"] == "staging"


def test_resource_contains_acs_cms_workspace(builder):
    assert builder.build_resource()["acs.cms.workspace"] == "iac-code"


def test_resource_contains_acs_arms_service_feature(builder):
    assert builder.build_resource()["acs.arms.service.feature"] == "genai_app"


def test_event_attributes_has_name_and_timestamp(builder):
    attrs = builder.build_event("iac.test.happened")
    assert attrs["event.name"] == "iac.test.happened"
    assert "event.timestamp" in attrs


def test_event_sequence_monotonically_increases_within_instance(builder):
    a = builder.build_event("iac.a")["event.sequence"]
    b = builder.build_event("iac.b")["event.sequence"]
    c = builder.build_event("iac.c")["event.sequence"]
    assert b == a + 1
    assert c == b + 1


def test_event_sequence_starts_fresh_per_instance(identity):
    b1 = AttributeBuilder(identity, "iac-code", "0.1.0")
    b2 = AttributeBuilder(identity, "iac-code", "0.1.0")
    assert b1.build_event("x")["event.sequence"] == b2.build_event("y")["event.sequence"]
