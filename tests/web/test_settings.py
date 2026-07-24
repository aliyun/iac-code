from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from iac_code.config import _load_yaml, _save_yaml, get_settings_path
from iac_code.services.providers import aliyun as aliyun_module
from iac_code.web import settings as settings_module
from iac_code.web import settings as web_settings


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("IAC_CODE_PROVIDER", "IAC_CODE_MODEL", "IAC_CODE_BASE_URL", "IAC_CODE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # 隔离:清除本机可能存在的阿里云环境凭证,避免 detected 探测受宿主环境影响。
    for name in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABA_CLOUD_SECURITY_TOKEN",
        "ALIBABA_CLOUD_REGION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    # 隔离:将 aliyun CLI 配置路径指向不存在的临时文件,避免读到本机 ~/.aliyun/config.json。
    monkeypatch.setattr(
        aliyun_module,
        "DEFAULT_ALIYUN_CLI_CONFIG_PATH",
        str(tmp_path / "no-aliyun" / "config.json"),
    )
    # 隔离:默认无本地可用第三方来源,避免依赖本机 ~/.qwenpaw.secret 等用户配置。
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [])

    from iac_code.web.app import create_app

    return create_app()


def test_get_providers_returns_capabilities_and_active_summary_without_secret(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/providers")

    assert response.status_code == 200
    data = response.json()
    assert data["active"] == {
        "provider": None,
        "model": None,
        "effort": None,
        "apiBase": None,
        "hasApiKey": False,
    }
    openai = next(provider for provider in data["providers"] if provider["key"] == "openai")
    assert openai["name"] == "OpenAI"
    assert openai["hasApiKey"] is False
    assert openai["configured"] is False
    assert "gpt-5.5" in [model["id"] for model in openai["models"]]
    assert "high" in next(model["efforts"] for model in openai["models"] if model["id"] == "gpt-5.5")
    assert "apiKey" not in response.text
    assert "sk-" not in response.text


def test_put_active_provider_persists_fake_values_and_exposes_saved_key_for_local_view(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        config_response = client.put(
            "/api/providers/config",
            json={
                "provider": "openai",
                "model": "gpt-5.5",
                "effort": "high",
                "apiBase": "https://llm.example.test/v1",
                "apiKey": "sk-fake-provider-secret",
            },
        )
        activate_response = client.put("/api/providers/active", json={"provider": "openai"})
        get_response = client.get("/api/providers")

    assert config_response.status_code == 200
    # 仅保存配置,尚未激活
    assert config_response.json()["active"]["provider"] is None
    saved_openai = next(provider for provider in config_response.json()["providers"] if provider["key"] == "openai")
    assert saved_openai["hasApiKey"] is True
    assert saved_openai["configured"] is True
    assert saved_openai["savedModel"] == "gpt-5.5"

    assert activate_response.status_code == 200
    assert activate_response.json()["active"] == {
        "provider": "openai",
        "model": "gpt-5.5",
        "effort": "high",
        "apiBase": "https://llm.example.test/v1",
        "hasApiKey": True,
    }
    assert get_response.status_code == 200
    assert get_response.json()["active"] == activate_response.json()["active"]
    openai = next(provider for provider in get_response.json()["providers"] if provider["key"] == "openai")
    assert openai["hasApiKey"] is True
    assert openai["configured"] is True

    # active 摘要保持精简:不回传明文密钥。
    assert "sk-fake-provider-secret" not in activate_response.text
    # 本地单用户工作台有意回填 savedApiKey,便于在页面以密文形式查看/编辑已存密钥。
    assert saved_openai["savedApiKey"] == "sk-fake-provider-secret"
    assert openai["savedApiKey"] == "sk-fake-provider-secret"
    # 请求态字段名 apiKey 不应出现在任一响应中(仅 hasApiKey / savedApiKey)。
    assert "apiKey" not in config_response.text
    assert "apiKey" not in activate_response.text
    assert "apiKey" not in get_response.text


def test_provider_config_and_active_routes_reject_bad_input(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        malformed = client.put("/api/providers/active", content="{")
        unknown_provider = client.put(
            "/api/providers/active",
            json={"provider": "not-a-provider"},
        )
        unknown_model = client.put(
            "/api/providers/config",
            json={"provider": "openai", "model": "not-a-model"},
        )
        unconfigured = client.put("/api/providers/active", json={"provider": "openai"})

    assert malformed.status_code == 400
    assert malformed.headers["content-type"].startswith("application/json")
    assert malformed.json()["error"]["message"] == "malformed JSON request body"
    assert unknown_provider.status_code == 400
    assert unknown_provider.json()["error"]["message"] == "unknown provider"
    assert unknown_model.status_code == 400
    assert unknown_model.json()["error"]["message"] == "unknown model"
    assert unconfigured.status_code == 400
    assert unconfigured.json()["error"]["message"] == "provider is not configured"


def test_get_cloud_aliyun_reports_unconfigured_without_secrets(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/cloud/aliyun")

    assert response.status_code == 200
    # 未配置时摘要携带凭证键但全为 null——本地工作台有意回填已存凭证(见 savedApiKey 约定),
    # 未配置即无任何明文可泄露,故整字典等值即证明「无密钥」。
    assert response.json() == {
        "configured": False,
        "mode": None,
        "region": None,
        "expiration": None,
        "oauthSiteType": None,
        "oauthAccessTokenExpire": None,
        "oauthRefreshTokenExpire": None,
        "stsExpiration": None,
        "accessKeyId": None,
        "accessKeySecret": None,
        "stsToken": None,
        "ramRoleArn": None,
        "ramSessionName": None,
        "detected": None,
    }
    # OAuth 访问/刷新令牌不参与回填,任何时候都不应出现在摘要里。
    for value_field in ('"oauthAccessToken"', '"oauthRefreshToken"'):
        assert value_field not in response.text


def test_get_cloud_aliyun_detected_from_env(monkeypatch, tmp_path):
    # _app 会清除本机阿里云环境变量以做隔离;detected 在请求时读取环境,
    # 故须在 _app 之后再注入伪造的环境凭证。
    client = TestClient(_app(monkeypatch, tmp_path))
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "LTAI-fake-id-1234")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "fake-secret-value")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    body = client.get("/api/cloud/aliyun").json()
    detected = body["detected"]
    assert detected["source"] == "env"
    assert detected["region"] == "cn-shanghai"
    assert detected["accessKeyId"] == "LTAI****"
    assert detected["hasAccessKeySecret"] is True
    text = client.get("/api/cloud/aliyun").text
    assert "fake-secret-value" not in text


def test_get_cloud_aliyun_detected_none_when_empty(monkeypatch, tmp_path):
    client = TestClient(_app(monkeypatch, tmp_path))
    body = client.get("/api/cloud/aliyun").json()
    assert body["detected"] is None
    assert body["configured"] is False


def test_put_cloud_aliyun_persists_fake_ak_and_exposes_saved_secret_for_local_view(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        put_response = client.put(
            "/api/cloud/aliyun",
            json={
                "mode": "AK",
                "region": "cn-shanghai",
                "accessKeyId": "LTAI-fake",
                "accessKeySecret": "fake-access-key-secret",
            },
        )
        get_response = client.get("/api/cloud/aliyun")

    # 本地单用户工作台有意回填已存凭证(与模型面板 savedApiKey 一致),便于在页面查看/编辑。
    expected = {
        "configured": True,
        "mode": "AK",
        "region": "cn-shanghai",
        "expiration": None,
        "oauthSiteType": None,
        "oauthAccessTokenExpire": None,
        "oauthRefreshTokenExpire": None,
        "stsExpiration": None,
        "accessKeyId": "LTAI-fake",
        "accessKeySecret": "fake-access-key-secret",
        "stsToken": None,
        "ramRoleArn": None,
        "ramSessionName": None,
    }
    assert put_response.status_code == 200
    assert put_response.json() == expected
    assert get_response.status_code == 200
    get_body = get_response.json()
    assert {k: get_body[k] for k in expected} == expected
    assert get_body["detected"] is not None and get_body["detected"]["source"] == "config"
    # 已保存的 AccessKeySecret 有意回传供本地查看;OAuth 令牌不参与回填(上面全量字典相等已保证摘要不含该字段)。
    assert put_response.json()["accessKeySecret"] == "fake-access-key-secret"
    assert get_body["accessKeySecret"] == "fake-access-key-secret"


def test_put_cloud_aliyun_preserves_omitted_existing_secret_fields(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        first_response = client.put(
            "/api/cloud/aliyun",
            json={
                "mode": "AK",
                "region": "cn-shanghai",
                "accessKeyId": "LTAI-fake",
                "accessKeySecret": "fake-access-key-secret",
            },
        )
        second_response = client.put(
            "/api/cloud/aliyun",
            json={
                "mode": "AK",
                "region": "cn-beijing",
            },
        )

    from iac_code.config import _load_yaml, get_cloud_credentials_path

    persisted = _load_yaml(get_cloud_credentials_path())["aliyun"]
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == {
        "configured": True,
        "mode": "AK",
        "region": "cn-beijing",
        "expiration": None,
        "oauthSiteType": None,
        "oauthAccessTokenExpire": None,
        "oauthRefreshTokenExpire": None,
        "stsExpiration": None,
        "accessKeyId": "LTAI-fake",
        "accessKeySecret": "fake-access-key-secret",
        "stsToken": None,
        "ramRoleArn": None,
        "ramSessionName": None,
    }
    assert persisted["access_key_id"] == "LTAI-fake"
    assert persisted["access_key_secret"] == "fake-access-key-secret"
    # 省略 secret 的二次保存会保留旧值,摘要照常回填该值供本地查看(savedApiKey 约定)。
    assert second_response.json()["accessKeySecret"] == "fake-access-key-secret"


def test_put_cloud_aliyun_falls_back_to_detected_ak(monkeypatch, tmp_path):
    # _app 会清除本机阿里云环境变量以做隔离;save/detect 在请求时读取环境,
    # 故须在 _app 之后再注入伪造的环境凭证(与 test_get_cloud_aliyun_detected_from_env 一致)。
    client = TestClient(_app(monkeypatch, tmp_path))
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "LTAI-detected-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "detected-secret")
    resp = client.put("/api/cloud/aliyun", json={"mode": "AK", "region": "cn-beijing"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["mode"] == "AK"
    assert body["region"] == "cn-beijing"
    # 兜底的检测 AK 已落盘;与 savedApiKey 约定一致,摘要有意回传该值供本地查看/编辑。
    assert body["accessKeyId"] == "LTAI-detected-id"
    assert body["accessKeySecret"] == "detected-secret"


def test_put_cloud_aliyun_supports_fake_modes_and_reports_expiration(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    # 每个 case 追加 expected_creds:摘要按模式回填的 AK/STS/Ram 家族凭证(本地查看用)。
    cases = [
        (
            {
                "mode": "StsToken",
                "region": "cn-beijing",
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "stsToken": "fake-sts-token",
                "expiration": 1798794000,
            },
            "StsToken",
            1798794000,
            None,
            None,
            {
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "stsToken": "fake-sts-token",
                "ramRoleArn": None,
                "ramSessionName": None,
                # StsToken 模式保留 sts_expiration(由 expiration 键合并而来)。
                "stsExpiration": 1798794000,
            },
        ),
        (
            {
                "mode": "RamRoleArn",
                "region": "cn-hangzhou",
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "ramRoleArn": "acs:ram::123:role/fake",
                "ramSessionName": "fake-session",
            },
            "RamRoleArn",
            None,
            None,
            None,
            {
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "stsToken": None,
                "ramRoleArn": "acs:ram::123:role/fake",
                "ramSessionName": "fake-session",
                # RamRoleArn 模式裁剪 sts_expiration → None。
                "stsExpiration": None,
            },
        ),
        (
            {
                "mode": "OAuth",
                "region": "cn-shenzhen",
                "oauthSiteType": "China",
                "oauthAccessToken": "fake-oauth-token",
                "oauthRefreshToken": "fake-refresh-token",
                "oauthAccessTokenExpire": 1798790400,
                "accessKeyId": "fake-ak",
                "accessKeySecret": "fake-secret",
                "stsToken": "fake-sts-token",
                "stsExpiration": 1798794000,
            },
            "OAuth",
            # OAuth 模式下 AK/STS 残留字段被清空(纵深防御),故回报的是 OAuth 过期时间
            1798790400,
            # 访问令牌过期时间按秒级时间戳回报;本用例未带刷新令牌过期时间,故为 None。
            1798790400,
            None,
            # OAuth 模式清空 AK/STS,回填字段全为 None。
            {
                "accessKeyId": None,
                "accessKeySecret": None,
                "stsToken": None,
                "ramRoleArn": None,
                "ramSessionName": None,
                # OAuth 模式(PUT 保存路径)裁剪 sts_expiration → None;
                # 派生的 STS 由 oauth-login 路径持久化,不经此裁剪。
                "stsExpiration": None,
            },
        ),
    ]

    with TestClient(app) as client:
        for case in cases:
            (
                payload,
                expected_mode,
                expected_expiration,
                expected_access_expire,
                expected_refresh_expire,
                expected_creds,
            ) = case
            response = client.put("/api/cloud/aliyun", json=payload)
            get_response = client.get("/api/cloud/aliyun")
            assert response.status_code == 200
            assert response.json() == {
                "configured": True,
                "mode": expected_mode,
                "region": payload["region"],
                "expiration": expected_expiration,
                # 非 OAuth 模式不带站点 → None;OAuth 模式回显已保存的站点值。
                "oauthSiteType": payload.get("oauthSiteType"),
                "oauthAccessTokenExpire": expected_access_expire,
                "oauthRefreshTokenExpire": expected_refresh_expire,
                **expected_creds,
            }
            assert get_response.status_code == 200
            assert {k: get_response.json()[k] for k in ("configured", "mode", "region", "expiration")} == {
                k: response.json()[k] for k in ("configured", "mode", "region", "expiration")
            }
            # AK/STS/Ram 家族凭证在两条响应里都按 expected_creds 精确回填。
            for key, value in expected_creds.items():
                assert response.json()[key] == value
                assert get_response.json()[key] == value
            # OAuth 访问/刷新令牌永不回填;OAuth 模式下 AK/STS 被清空亦不应出现明文。
            assert "fake-oauth-token" not in response.text
            assert "fake-refresh-token" not in response.text
            assert "fake-oauth-token" not in get_response.text
            assert "fake-refresh-token" not in get_response.text
            if expected_creds["accessKeySecret"] is None:
                assert "fake-secret" not in response.text
                assert "fake-sts-token" not in response.text
                assert "fake-secret" not in get_response.text
                assert "fake-sts-token" not in get_response.text


def test_cloud_aliyun_rejects_bad_json_and_unknown_mode(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        malformed = client.put("/api/cloud/aliyun", content="[")
        unknown_mode = client.put("/api/cloud/aliyun", json={"mode": "Nope"})
        incomplete = client.put("/api/cloud/aliyun", json={"mode": "AK"})

    assert malformed.status_code == 400
    assert malformed.headers["content-type"].startswith("application/json")
    assert malformed.json()["error"]["message"] == "malformed JSON request body"
    assert unknown_mode.status_code == 400
    assert unknown_mode.json()["error"]["message"] == "unknown aliyun credential mode"
    assert incomplete.status_code == 400
    assert incomplete.json()["error"]["message"] == (
        "missing required aliyun credential fields: access_key_id, access_key_secret"
    )


def test_get_cloud_aliyun_rejects_malformed_stored_numeric_fields_without_echoing_value(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    from iac_code.config import _save_yaml, get_cloud_credentials_path

    _save_yaml(
        get_cloud_credentials_path(),
        {
            "aliyun": {
                "mode": "StsToken",
                "region_id": "cn-hangzhou",
                "access_key_id": "fake-ak",
                "access_key_secret": "fake-secret",
                "sts_token": "fake-token",
                "sts_expiration": "not-an-int-secret",
            }
        },
    )

    with TestClient(app) as client:
        response = client.get("/api/cloud/aliyun")

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "stored aliyun credentials are invalid"}}
    assert "not-an-int-secret" not in response.text
    assert "fake-secret" not in response.text
    assert "fake-token" not in response.text


@pytest.fixture()
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    # 隔离:默认无本地可用第三方来源,避免依赖本机用户配置。
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [])
    yield


def _expected_group_order() -> list[str]:
    from iac_code.providers.registry import PROVIDER_GROUPS, PROVIDER_REGISTRY

    keys: list[str] = []
    for _label, group_keys in PROVIDER_GROUPS:
        keys.extend(key for key in group_keys if key in PROVIDER_REGISTRY)
    return keys


def test_get_providers_orders_by_shared_groups_with_group_labels(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        data = client.get("/api/providers").json()

    providers = data["providers"]
    # 所有条目均为 provider 类型且带分组标签,顺序与共享的 PROVIDER_GROUPS 一致。
    assert [p["key"] for p in providers] == _expected_group_order()
    assert all(p["kind"] == "provider" for p in providers)
    assert all(isinstance(p["group"], str) and p["group"] for p in providers)
    # 同一分组的条目必须连续(不被打散)。
    seen_groups: list[str] = []
    for provider in providers:
        if not seen_groups or seen_groups[-1] != provider["group"]:
            seen_groups.append(provider["group"])
    assert len(seen_groups) == len(set(seen_groups))


class _FakePartner:
    key = "qwenpaw"
    display_name = "QwenPaw"

    def get_provider_display(self) -> str:
        return "DeepSeek"


def test_get_providers_prepends_available_partner_as_readonly(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [_FakePartner()])
    monkeypatch.setattr(web_settings, "get_llm_source", lambda: "qwenpaw")

    with TestClient(app) as client:
        data = client.get("/api/providers").json()

    providers = data["providers"]
    partner = providers[0]
    assert partner["key"] == "partner:qwenpaw"
    assert partner["kind"] == "partner"
    assert partner["readOnly"] is True
    assert partner["providerLabel"] == "DeepSeek"
    assert partner["current"] is True
    assert partner["configured"] is True
    assert partner["note"]
    assert partner["group"]
    # 只读伙伴条目不含可编辑表单字段。
    assert partner["models"] == []
    assert partner["savedApiKey"] is None
    # 其余条目仍为真实 provider,顺序不变。
    assert [p["key"] for p in providers[1:]] == _expected_group_order()


def _first_provider_key() -> str:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    for key, descriptor in PROVIDER_REGISTRY.items():
        if descriptor.model_ids:
            return key
    raise AssertionError("no provider with models in registry")


def _first_model(key: str) -> str:
    from iac_code.providers.registry import PROVIDER_REGISTRY

    return PROVIDER_REGISTRY[key].model_ids[0]


def test_save_provider_config_does_not_activate(_isolate_config):
    key = _first_provider_key()
    model = _first_model(key)

    payload = web_settings.save_provider_config({"provider": key, "model": model})

    config = _load_yaml(get_settings_path())
    assert config["providers"][key]["model"] == model
    # 关键:保存配置不得设置 activeProvider
    assert config.get("activeProvider") is None
    saved = next(p for p in payload["providers"] if p["key"] == key)
    assert saved["savedModel"] == model


def test_set_active_provider_requires_saved_config(_isolate_config):
    key = _first_provider_key()
    with pytest.raises(ValueError):
        web_settings.set_active_provider({"provider": key})


def test_set_active_provider_activates_configured_provider(_isolate_config):
    key = _first_provider_key()
    model = _first_model(key)
    web_settings.save_provider_config({"provider": key, "model": model})

    result = web_settings.set_active_provider({"provider": key})

    config = _load_yaml(get_settings_path())
    assert config["activeProvider"] == key
    assert result["active"]["provider"] == key
    assert result["active"]["model"] == model


def test_local_provider_not_usable_until_model_saved(_isolate_config):
    from iac_code.providers.registry import PROVIDER_REGISTRY

    # 本地模型无需密钥,但没有可用模型时不应「可用」(即不点亮绿点)。
    local_key = next(k for k, d in PROVIDER_REGISTRY.items() if d.is_local and not d.default_model)
    entry = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == local_key)
    assert entry["isLocal"] is True
    assert entry["hasApiKey"] is False
    assert entry["usable"] is False

    # 填入并保存模型后变为可用。
    web_settings.save_provider_config({"provider": local_key, "model": "my-local-model"})
    after = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == local_key)
    assert after["savedModel"] == "my-local-model"
    assert after["usable"] is True


def test_catalog_provider_requires_both_key_and_model_for_usable(_isolate_config):
    # 需密钥的目录服务商:有默认模型但缺密钥时不可用;补齐密钥后凭默认模型即可用。
    entry = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == "openai")
    assert entry["defaultModel"]
    assert entry["hasApiKey"] is False
    assert entry["usable"] is False

    web_settings.save_provider_config({"provider": "openai", "model": "gpt-5.5", "apiKey": "sk-fake-openai"})
    after = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == "openai")
    assert after["hasApiKey"] is True
    assert after["usable"] is True


def test_compatible_provider_with_key_but_no_model_is_not_usable(tmp_path, monkeypatch):
    # 兼容模式(openai_compatible):有密钥、无模型、无默认模型 → 不可用,不点亮绿点。
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [])
    monkeypatch.setattr(web_settings, "load_credentials", lambda **_: {"openai_compatible": "sk-fake-compatible"})

    entry = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == "openai_compatible")
    assert entry["hasApiKey"] is True
    assert entry["savedModel"] is None
    assert entry["defaultModel"] is None
    assert entry["usable"] is False


def test_compatible_provider_requires_api_base_for_usable(tmp_path, monkeypatch):
    # 兼容模式无默认端点:即便有密钥+模型,缺 base_url 仍不可用;补上 base_url 后才点亮绿点。
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [])
    monkeypatch.setattr(web_settings, "load_credentials", lambda **_: {"openai_compatible": "sk-fake-compatible"})

    web_settings.save_provider_config({"provider": "openai_compatible", "model": "custom-model"})
    without_base = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == "openai_compatible")
    assert without_base["savedModel"] == "custom-model"
    assert without_base["savedApiBase"] is None
    assert without_base["usable"] is False

    web_settings.save_provider_config(
        {"provider": "openai_compatible", "model": "custom-model", "apiBase": "https://api.example.com/v1"}
    )
    with_base = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == "openai_compatible")
    assert with_base["savedApiBase"] == "https://api.example.com/v1"
    assert with_base["usable"] is True


def test_local_provider_usable_without_saved_api_base(_isolate_config):
    # 本地模型注册表已带 localhost 默认端点:只要填了模型即可用,不强制保存 base_url。
    from iac_code.providers.registry import PROVIDER_REGISTRY

    local_key = next(k for k, d in PROVIDER_REGISTRY.items() if d.is_local and not d.default_model)
    # 仅填模型、未由用户单独提供 base_url,本地模型仍可用(端点回退到注册表 localhost 默认)。
    web_settings.save_provider_config({"provider": local_key, "model": "my-local-model"})
    entry = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == local_key)
    assert entry["savedModel"] == "my-local-model"
    assert entry["usable"] is True


def test_set_active_partner_switches_llm_source_and_drops_active_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [_FakePartner()])
    # 先激活一个普通 provider,验证切到第三方来源时 activeProvider 会被移除。
    key = _first_provider_key()
    model = _first_model(key)
    web_settings.save_provider_config({"provider": key, "model": model})
    web_settings.set_active_provider({"provider": key})

    result = web_settings.set_active_provider({"provider": "partner:qwenpaw"})

    config = _load_yaml(get_settings_path())
    # get_llm_source 优先级:activeProvider 高于 llm_source,故必须弹出 activeProvider。
    assert "activeProvider" not in config
    assert config["llm_source"] == "qwenpaw"
    assert "active" in result


def test_set_active_partner_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(web_settings, "get_available_partner_sources", lambda: [])
    with pytest.raises(ValueError):
        web_settings.set_active_provider({"provider": "partner:qwenpaw"})


def test_validate_effort_allows_free_input_when_no_spec(_isolate_config):
    # 无已知推理强度规格(如手动输入的模型)时,任意合法强度都应放行,
    # 以支持前端组合框的自由输入。
    web_settings._validate_effort("openrouter", "some/manually-typed-model", "high")


def test_validate_effort_still_enforces_known_spec(_isolate_config):
    # 有已知规格(openai/gpt-5.5 有固定 effort 列表)时仍强制校验,拒绝规格外的值。
    with pytest.raises(ValueError):
        web_settings._validate_effort("openai", "gpt-5.5", "definitely-not-an-effort")


def test_provider_payload_exposes_saved_fields(_isolate_config):
    key = _first_provider_key()
    model = _first_model(key)
    web_settings.save_provider_config({"provider": key, "model": model, "apiBase": "https://override.test/v1"})

    payload = web_settings.providers_payload()
    entry = next(p for p in payload["providers"] if p["key"] == key)
    assert entry["savedModel"] == model
    assert entry["savedApiBase"] == "https://override.test/v1"
    # 未写入数值上限时,两个 saved 值应为 None(前端据此留空输入框)。
    assert entry["savedThinkingBudget"] is None
    assert entry["savedMaxCompletionTokens"] is None
    # 未保存 saved 值的其它 provider 应为 None
    other = next(p for p in payload["providers"] if p["key"] != key)
    assert other["savedModel"] is None
    assert other["savedThinkingBudget"] is None
    assert other["savedMaxCompletionTokens"] is None


def test_model_payload_exposes_family_aware_thinking_default(_isolate_config):
    # 新会话草稿据此点亮「思考」按钮:dashscope 家族 unset 默认思考,reasoning 家族默认关。
    payload = web_settings.providers_payload()

    def _default_model(provider_key: str) -> dict:
        entry = next(p for p in payload["providers"] if p["key"] == provider_key)
        assert entry["models"], f"{provider_key} 应有枚举模型"
        return next((m for m in entry["models"] if m["default"]), entry["models"][0])

    assert _default_model("dashscope")["thinkingDefault"] is True
    assert _default_model("openai")["thinkingDefault"] is False


def test_model_payload_exposes_thinking_budget_capability(_isolate_config):
    # 「思考预算」字段能力门控:仅 supports_thinking_budget 的模型可见,前端据此显隐。
    payload = web_settings.providers_payload()

    def _model(provider_key: str, model_id: str) -> dict:
        entry = next(p for p in payload["providers"] if p["key"] == provider_key)
        return next(m for m in entry["models"] if m["id"] == model_id)

    glm = _model("dashscope", "glm-5.2")
    assert glm["supportsThinkingBudget"] is True
    assert glm["defaultThinkingBudget"] == 8192
    kimi = _model("dashscope", "kimi-k2.7-code")
    assert kimi["supportsThinkingBudget"] is True
    # 效应驱动家族用 effort 控件,不暴露独立预算字段。
    gpt = _model("openai", "gpt-5.5")
    assert gpt["supportsThinkingBudget"] is False
    assert gpt["defaultThinkingBudget"] is None
    # 「最大输出 tokens」对所有模型生效,均暴露留空回落默认(前端 placeholder 展示)。
    assert glm["defaultMaxCompletionTokens"] == 8192
    assert gpt["defaultMaxCompletionTokens"] == 8192


def test_save_provider_config_round_trips_output_and_budget_knobs(_isolate_config):
    # glm-5.2 两个开关都真正生效(supports_thinking_budget + use_max_completion_tokens)。
    # 两个旋钮按模型存于 providers.<key>.models.<id> 下,而非 provider 顶层。
    web_settings.save_provider_config(
        {
            "provider": "dashscope",
            "model": "glm-5.2",
            "thinkingBudget": 2048,
            "maxCompletionTokens": 40000,
        }
    )
    entry = _load_yaml(get_settings_path())["providers"]["dashscope"]
    assert "thinkingBudget" not in entry
    assert "maxCompletionTokens" not in entry
    model_entry = entry["models"]["glm-5.2"]
    assert model_entry["thinkingBudget"] == 2048
    assert model_entry["maxCompletionTokens"] == 40000
    payload = web_settings.providers_payload()
    provider = next(p for p in payload["providers"] if p["key"] == "dashscope")
    model = next(m for m in provider["models"] if m["id"] == "glm-5.2")
    assert model["savedThinkingBudget"] == 2048
    assert model["savedMaxCompletionTokens"] == 40000


def test_save_provider_config_knobs_are_per_model(_isolate_config):
    # 同一 provider 下不同模型互不干扰:给 glm-5.2 设值不应波及 kimi-k2.7-code。
    web_settings.save_provider_config(
        {"provider": "dashscope", "model": "glm-5.2", "maxCompletionTokens": 40000}
    )
    web_settings.save_provider_config(
        {"provider": "dashscope", "model": "kimi-k2.7-code", "maxCompletionTokens": 12000}
    )
    models_cfg = _load_yaml(get_settings_path())["providers"]["dashscope"]["models"]
    assert models_cfg["glm-5.2"]["maxCompletionTokens"] == 40000
    assert models_cfg["kimi-k2.7-code"]["maxCompletionTokens"] == 12000
    payload = web_settings.providers_payload()
    provider = next(p for p in payload["providers"] if p["key"] == "dashscope")
    glm = next(m for m in provider["models"] if m["id"] == "glm-5.2")
    kimi = next(m for m in provider["models"] if m["id"] == "kimi-k2.7-code")
    assert glm["savedMaxCompletionTokens"] == 40000
    assert kimi["savedMaxCompletionTokens"] == 12000


def test_save_provider_config_null_clears_knob_but_preserves_others(_isolate_config):
    # 先写入两个值,再以 null 清除 thinkingBudget:该键从模型条目删除,其余键保留(回落模型默认)。
    web_settings.save_provider_config(
        {
            "provider": "dashscope",
            "model": "glm-5.2",
            "thinkingBudget": 2048,
            "maxCompletionTokens": 40000,
        }
    )
    web_settings.save_provider_config(
        {
            "provider": "dashscope",
            "model": "glm-5.2",
            "thinkingBudget": None,
            "maxCompletionTokens": 30000,
        }
    )
    model_entry = _load_yaml(get_settings_path())["providers"]["dashscope"]["models"]["glm-5.2"]
    assert "thinkingBudget" not in model_entry
    assert model_entry["maxCompletionTokens"] == 30000


def test_save_provider_config_absent_fields_preserve_existing_knobs(_isolate_config):
    # 请求里不带该字段(_UNSET)时保持现状:仅改 effort 不应清掉已存的数值上限。
    web_settings.save_provider_config(
        {
            "provider": "dashscope",
            "model": "glm-5.2",
            "thinkingBudget": 2048,
            "maxCompletionTokens": 40000,
        }
    )
    web_settings.save_provider_config({"provider": "dashscope", "model": "glm-5.2", "effort": "low"})
    entry = _load_yaml(get_settings_path())["providers"]["dashscope"]
    # effort 仍存 provider 顶层;两个数值旋钮在模型条目里保持不变。
    assert entry["effort"] == "low"
    model_entry = entry["models"]["glm-5.2"]
    assert model_entry["thinkingBudget"] == 2048
    assert model_entry["maxCompletionTokens"] == 40000


@pytest.mark.parametrize("bad", [0, -5, "big", True])
def test_save_provider_config_rejects_non_positive_or_non_int_knobs(_isolate_config, bad):
    with pytest.raises(ValueError):
        web_settings.save_provider_config(
            {"provider": "dashscope", "model": "glm-5.2", "maxCompletionTokens": bad}
        )
    with pytest.raises(ValueError):
        web_settings.save_provider_config(
            {"provider": "dashscope", "model": "glm-5.2", "thinkingBudget": bad}
        )


def test_save_active_provider_persists_output_and_budget_knobs(_isolate_config):
    web_settings.save_active_provider(
        {
            "provider": "dashscope",
            "model": "glm-5.2",
            "thinkingBudget": 4096,
            "maxCompletionTokens": 50000,
        }
    )
    config = _load_yaml(get_settings_path())
    assert config["activeProvider"] == "dashscope"
    model_entry = config["providers"]["dashscope"]["models"]["glm-5.2"]
    assert model_entry["thinkingBudget"] == 4096
    assert model_entry["maxCompletionTokens"] == 50000


def test_clear_provider_config_removes_saved_config_and_key(_isolate_config):
    from iac_code.config import get_credentials_path

    key = _first_provider_key()
    model = _first_model(key)
    web_settings.save_provider_config(
        {"provider": key, "model": model, "apiBase": "https://override.test/v1", "apiKey": "sk-fake-clear"}
    )
    configured = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == key)
    assert configured["savedModel"] == model
    assert configured["hasApiKey"] is True
    assert configured["usable"] is True

    payload = web_settings.clear_provider_config({"provider": key})

    # 整条重置:回到未配置状态(无模型、无密钥、绿点熄灭)。
    cleared = next(p for p in payload["providers"] if p["key"] == key)
    assert cleared["savedModel"] is None
    assert cleared["hasApiKey"] is False
    assert cleared["usable"] is False
    # settings.yml 不再残留该 provider 配置,凭证文件中的密钥也已删除。
    settings_config = _load_yaml(get_settings_path())
    assert key not in settings_config.get("providers", {})
    assert key not in _load_yaml(get_credentials_path())


def test_clear_provider_config_refuses_active_provider(_isolate_config):
    # 禁止清空当前激活的 provider:须先切换到其它模型。
    key = _first_provider_key()
    model = _first_model(key)
    web_settings.save_provider_config({"provider": key, "model": model, "apiKey": "sk-fake-active"})
    web_settings.set_active_provider({"provider": key})

    with pytest.raises(ValueError, match="cannot clear active provider"):
        web_settings.clear_provider_config({"provider": key})

    # 拒绝后配置与密钥应原样保留。
    still = next(p for p in web_settings.providers_payload()["providers"] if p["key"] == key)
    assert still["savedModel"] == model
    assert still["hasApiKey"] is True


def test_login_aliyun_oauth_persists_and_summarizes(monkeypatch, tmp_path):
    from iac_code.services.providers import aliyun_oauth as oauth_module

    fake_token = oauth_module.OAuthToken(
        access_token="fake-access",
        refresh_token="fake-refresh",
        access_token_expire=1798790400,
        refresh_token_expire=1801382400,
    )
    monkeypatch.setattr(oauth_module, "run_browser_oauth_flow", lambda site, **_: fake_token)
    monkeypatch.setattr(settings_module, "run_browser_oauth_flow", lambda site, **_: fake_token, raising=False)

    # 模拟登录后派生 STS 临时凭证(真实流程见 refresh_oauth_if_needed):设置 sts_expiration,
    # 以验证摘要独立回传该值(供前端在 OAuth 面板展示第三行「STS 过期」)。
    def _derive_sts(cred):
        cred.sts_expiration = 1798794000
        return cred

    monkeypatch.setattr(aliyun_module.AliyunCredentials, "refresh_oauth_if_needed", staticmethod(_derive_sts))
    client = TestClient(_app(monkeypatch, tmp_path))
    resp = client.post("/api/cloud/aliyun/oauth-login", json={"site": "CN", "region": "cn-hangzhou"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["mode"] == "OAuth"
    # 登录站点(CN/INTL)随摘要返回,供前端回填「登录站点」选择框。
    assert body["oauthSiteType"] == "CN"
    # 令牌过期时间(秒级 Unix 时间戳)随摘要返回,供前端按本地时区展示。
    assert body["oauthAccessTokenExpire"] == 1798790400
    assert body["oauthRefreshTokenExpire"] == 1801382400
    # 登录派生的 STS 到期时间独立于两个令牌,随摘要单独回传。
    assert body["stsExpiration"] == 1798794000
    assert "fake-access" not in resp.text


def test_login_aliyun_oauth_reports_failure(monkeypatch, tmp_path):
    from iac_code.services.providers import aliyun_oauth as oauth_module

    def _boom(site, **_):
        raise oauth_module.AliyunOAuthError("login failed")

    monkeypatch.setattr(settings_module, "run_browser_oauth_flow", _boom, raising=False)
    monkeypatch.setattr(oauth_module, "run_browser_oauth_flow", _boom)
    client = TestClient(_app(monkeypatch, tmp_path))
    resp = client.post("/api/cloud/aliyun/oauth-login", json={"site": "CN"})
    assert resp.status_code == 400
    assert "login failed" in resp.json()["error"]["message"]


def test_prune_aliyun_credential_oauth_clears_ak_sts() -> None:
    cred = aliyun_module.AliyunCredential(
        mode="OAuth",
        access_key_id="LTAI-fake",
        access_key_secret="fake-secret",
        sts_token="fake-sts-token",
        sts_expiration=1234567890,
        ram_role_arn="acs:ram::fake:role/fake",
        ram_session_name="fake-session",
        oauth_site_type="CN",
        oauth_access_token="fake-oauth-access",
        oauth_refresh_token="fake-oauth-refresh",
        oauth_access_token_expire=111,
        oauth_refresh_token_expire=222,
    )
    web_settings._prune_aliyun_credential_for_mode(cred)
    assert cred.access_key_id == ""
    assert cred.access_key_secret == ""
    assert cred.sts_token == ""
    assert cred.sts_expiration == 0
    assert cred.ram_role_arn == ""
    assert cred.ram_session_name == ""
    assert cred.oauth_site_type == "CN"
    assert cred.oauth_access_token == "fake-oauth-access"
    assert cred.oauth_refresh_token == "fake-oauth-refresh"
    assert cred.oauth_access_token_expire == 111
    assert cred.oauth_refresh_token_expire == 222


@pytest.fixture()
def _restore_process_language():
    # save_ui_language calls set_language, which rebinds process-global gettext.
    # Snapshot and restore so language-sensitive assertions in other tests/files
    # are not affected by tests here that persist a non-English language.
    from iac_code import i18n

    previous = i18n._current_language
    yield
    i18n.set_language(previous)


def test_ui_language_round_trip(tmp_path, monkeypatch, _restore_process_language):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    assert web_settings.get_ui_language() is None
    assert web_settings.save_ui_language("fr") == {"language": "fr"}
    assert web_settings.get_ui_language() == "fr"


def test_ui_language_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        web_settings.save_ui_language("klingon")


def test_ui_language_preserves_other_ui_keys(tmp_path, monkeypatch, _restore_process_language):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    path = get_settings_path()
    data = _load_yaml(path)
    data.setdefault("ui", {})["someOtherKey"] = "keep-me"
    _save_yaml(path, data)
    web_settings.save_ui_language("de")
    reloaded = _load_yaml(get_settings_path())
    assert reloaded["ui"]["someOtherKey"] == "keep-me"
    assert reloaded["ui"]["language"] == "de"


def test_ui_language_payload_exposes_available(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    payload = web_settings.ui_language_payload()
    assert payload["uiLanguage"] is None
    assert {"code": "en", "name": "English"} in payload["availableLanguages"]
    assert len(payload["availableLanguages"]) == 7


def test_run_web_server_applies_persisted_ui_language(monkeypatch, _restore_process_language):
    # At web-server startup the persisted UI language must be applied process-globally,
    # so backend _() responses render in the chosen language (not just the browser chrome).
    import sys
    from types import SimpleNamespace

    import iac_code.i18n as i18n
    import iac_code.web.server as server

    monkeypatch.setattr("iac_code.web.server.get_ui_language", lambda: "de")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: None))

    server.run_web_server(host="127.0.0.1", port=8766, open_browser=False)

    assert i18n.get_current_language() == "de"
