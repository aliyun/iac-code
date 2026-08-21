from __future__ import annotations

import json

from iac_code.services.configuration_readiness import configuration_readiness
from iac_code.services.providers.aliyun import AliyunCredentials


def _isolate_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    for name in (
        "IAC_CODE_API_KEY",
        "IAC_CODE_PROVIDER",
        "IAC_CODE_MODEL",
        "IAC_CODE_BASE_URL",
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABA_CLOUD_SECURITY_TOKEN",
        "ALIBABA_CLOUD_REGION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(AliyunCredentials, "_load_from_aliyun_cli", staticmethod(lambda _path=None: None))


def test_readiness_reports_complete_llm_and_cloud_configuration_without_secret_values(monkeypatch, tmp_path) -> None:
    _isolate_configuration(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yml").write_text(
        "activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.6\n",
        encoding="utf-8",
    )
    (config_dir / ".credentials.yml").write_text("openai: llm-secret-value\n", encoding="utf-8")
    (config_dir / ".cloud-credentials.yml").write_text(
        "aliyun:\n  mode: AK\n  access_key_id: cloud-id\n  access_key_secret: cloud-secret-value\n"
        "  region_id: cn-shanghai\n",
        encoding="utf-8",
    )

    result = configuration_readiness(model="gpt-5.6")

    assert result["llm"] == {
        "ready": True,
        "source": "local",
        "provider": "openai",
        "providerDisplay": "OpenAI",
        "model": "gpt-5.6",
        "missing": [],
    }
    assert result["cloud"] == {
        "ready": True,
        "provider": "aliyun",
        "mode": "AK",
        "regionId": "cn-shanghai",
        "missing": [],
    }
    rendered = json.dumps(result)
    assert "llm-secret-value" not in rendered
    assert "cloud-secret-value" not in rendered
    assert "cloud-id" not in rendered


def test_readiness_reports_missing_llm_key_and_incomplete_cloud_fields(monkeypatch, tmp_path) -> None:
    _isolate_configuration(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yml").write_text(
        "activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.6\n",
        encoding="utf-8",
    )
    (config_dir / ".cloud-credentials.yml").write_text(
        "aliyun:\n  mode: AK\n  access_key_id: only-id\n  region_id: cn-hangzhou\n",
        encoding="utf-8",
    )

    result = configuration_readiness(model="gpt-5.6")

    assert result["llm"]["ready"] is False
    assert result["llm"]["missing"] == ["api_key"]
    assert result["cloud"]["ready"] is False
    assert result["cloud"]["missing"] == ["access_key_secret"]


def test_readiness_treats_ecs_ram_role_as_complete_without_static_keys(monkeypatch, tmp_path) -> None:
    _isolate_configuration(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".cloud-credentials.yml").write_text(
        "aliyun:\n  mode: EcsRamRole\n  region_id: cn-hangzhou\n",
        encoding="utf-8",
    )

    result = configuration_readiness(model="qwen3.8-max")

    assert result["cloud"]["ready"] is True
    assert result["cloud"]["mode"] == "EcsRamRole"
    assert result["cloud"]["missing"] == []


def test_readiness_treats_oauth_refresh_token_expiry_as_optional(monkeypatch, tmp_path) -> None:
    _isolate_configuration(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".cloud-credentials.yml").write_text(
        "aliyun:\n"
        "  mode: OAuth\n"
        "  region_id: cn-hangzhou\n"
        "  oauth_site_type: CN\n"
        "  oauth_access_token: access-token\n"
        "  oauth_refresh_token: refresh-token\n"
        "  oauth_access_token_expire: 1801382400\n"
        "  access_key_id: sts-access-key-id\n"
        "  access_key_secret: sts-access-key-secret\n"
        "  sts_token: sts-token\n"
        "  sts_expiration: 1801382400\n",
        encoding="utf-8",
    )

    result = configuration_readiness(model="qwen3.8-max")

    assert result["cloud"] == {
        "ready": True,
        "provider": "aliyun",
        "mode": "OAuth",
        "regionId": "cn-hangzhou",
        "missing": [],
    }
