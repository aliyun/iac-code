"""Test credential exports use fake data and never replace the source or its links."""

from __future__ import annotations

import os

import pytest
import yaml

from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from scripts.ci.cloud_test_credentials import export


@pytest.mark.parametrize("destination", ["entry", "target", "alias", "dangling"])
def test_export_rejects_the_source_entry_and_canonical_target(tmp_path, monkeypatch, destination):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "oauth.yml"
    target.write_text("fake durable OAuth", encoding="utf-8")
    entry = source / ".cloud-credentials.yml"
    if destination == "entry":
        entry.write_text("fake source", encoding="utf-8")
        output = entry
    else:
        if destination == "dangling":
            target.unlink()
        entry.symlink_to(target)
        output = target if destination == "target" else entry
        if destination == "alias":
            output = tmp_path / "output-link.yml"
            output.symlink_to(entry)
    monkeypatch.setenv("IAC_CODE_E2E_CLOUD_SOURCE", str(source))
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "unchanged-config"))

    def unexpected_load():
        pytest.fail("self-overwrite must be rejected before loading or refreshing credentials")

    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(unexpected_load))
    with pytest.raises(ValueError, match="must not replace"):
        export(output)
    assert os.environ["IAC_CODE_CONFIG_DIR"] == str(tmp_path / "unchanged-config")
    if destination == "entry":
        assert entry.read_text(encoding="utf-8") == "fake source"
    else:
        assert entry.is_symlink() and entry.readlink() == target
        if destination == "dangling":
            assert not target.exists()
        else:
            assert target.read_text(encoding="utf-8") == "fake durable OAuth"
        if destination == "alias":
            assert output.is_symlink()


def test_export_to_separate_file_keeps_the_source_symlink(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "oauth.yml"
    target.write_text("fake durable OAuth", encoding="utf-8")
    entry = source / ".cloud-credentials.yml"
    entry.symlink_to(target)
    monkeypatch.setenv("IAC_CODE_E2E_CLOUD_SOURCE", str(source))
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "initial-config"))
    credential = AliyunCredential(
        mode="OAuth", access_key_id="fixture-ak", access_key_secret="fixture-secret",
        sts_token="fixture-sts", oauth_refresh_token="fixture-rotating-token",
    )
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: credential))
    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", staticmethod(lambda value: value))
    output = tmp_path / "case" / "sts.yml"
    export(output)
    payload = yaml.safe_load(output.read_text(encoding="utf-8"))["aliyun"]
    assert payload["mode"] == "StsToken" and payload["sts_token"] == "fixture-sts"
    assert not any(key.startswith("oauth") for key in payload)
    assert entry.is_symlink() and target.read_text(encoding="utf-8") == "fake durable OAuth"
