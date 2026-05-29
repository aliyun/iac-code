"""Tests for the Alibaba Cloud OpenAPI User-Agent builder."""

import re

import pytest

from iac_code.tools.cloud.aliyun.user_agent import build_user_agent


def test_released_build_includes_release_date(monkeypatch):
    monkeypatch.setattr("iac_code.__version__", "0.3.0")
    monkeypatch.setattr("iac_code.__release_date__", "2026-01-15")
    ua = build_user_agent()
    assert ua.startswith("iac-code/0.3.0+2026-01-15 (")
    assert ua.endswith(")")


def test_local_build_uses_dev_suffix(monkeypatch):
    monkeypatch.setattr("iac_code.__version__", "0.3.0")
    monkeypatch.setattr("iac_code.__release_date__", "")
    ua = build_user_agent()
    assert ua.startswith("iac-code/0.3.0+dev (")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_release_date_normalizes_to_dev(monkeypatch, blank):
    monkeypatch.setattr("iac_code.__release_date__", blank)
    assert "+dev " in build_user_agent()


def test_darwin_normalized_to_macos(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("platform.python_version", lambda: "3.10.4")
    monkeypatch.setattr("iac_code.__release_date__", "2026-01-15")
    monkeypatch.setattr("iac_code.__version__", "0.3.0")
    assert build_user_agent() == "iac-code/0.3.0+2026-01-15 (macOS; arm64; Python/3.10.4)"


@pytest.mark.parametrize(
    "system,expected_os",
    [("Linux", "Linux"), ("Windows", "Windows"), ("", "unknown")],
)
def test_non_darwin_systems_kept_as_is(monkeypatch, system, expected_os):
    monkeypatch.setattr("platform.system", lambda: system)
    ua = build_user_agent()
    assert f"({expected_os};" in ua


def test_missing_machine_falls_back_to_unknown(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "")
    ua = build_user_agent()
    assert re.search(r"\(\w+; unknown; Python/", ua)
