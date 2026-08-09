from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

import iac_code.tools.cloud.aliyun.endpoint_resolver as endpoint_resolver_module
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError, CanonicalWireContract, RequestBuilder
from iac_code.tools.cloud.aliyun.api_identifiers import SAFE_API_VERSION
from iac_code.tools.cloud.aliyun.endpoint_resolver import (
    AccountIdentityResolver,
    EndpointResolutionError,
    EndpointResolver,
    HostBindingResolver,
    LocationResolver,
    merge_endpoint_record,
)
from iac_code.tools.cloud.aliyun.openmeta import ParameterMetadata
from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services
from tests.tools.cloud.aliyun._ecs_ram_role_fakes import FakeEcsRuntime

ROOT = Path(__file__).parents[4]
PACKAGE = ROOT / "src/iac_code/tools/cloud/aliyun"
ENDPOINT_DATA = PACKAGE / "data/endpoints"
SOURCE = Path(__file__).parent / "fixtures/endpoints/aliyun-openapi-meta/metadatas/products.json"
OPENMETA_PRODUCTS = Path(__file__).parent / "fixtures/endpoints/openmeta_products.json"
COMMIT = "2563691c22229a0b493606e11166b95896707095"
SOURCE_SHA256 = "e79346fbe87dbacd73c4cb68520f897add17a8e90cadb8fb03e5efa217d04be5"
SOURCE_REPOSITORY = "https://github.com/aliyun/aliyun-openapi-meta.git"
OPENMETA_PRODUCTS_URL = "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN"
OPENMETA_PRODUCT_COUNT = 339
OPENMETA_PRODUCTS_SHA256 = "d226cf4dfe48636261a08cd3b6e94e422831aa0f8e15c08e391023784e27830f"
PRODUCT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class FakeLocation:
    def __init__(self, results: list[str | None | Exception]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, str, str, Any]] = []

    async def resolve(
        self, product: str, version: str, region_id: str, service_code: str, credential: Any
    ) -> str | None:
        self.calls.append((product, version, region_id, service_code, credential))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_account_identity_resolver_validates_and_caches_account_id() -> None:
    calls: list[tuple[str, Any]] = []

    async def request(host: str, value: Any) -> dict[str, str]:
        calls.append((host, value))
        return {"AccountId": "1234567890123456"}

    resolver = AccountIdentityResolver(request)
    assert await resolver.resolve("credential", "cn-hangzhou") == "1234567890123456"
    assert await resolver.resolve("credential", "cn-hangzhou") == "1234567890123456"
    assert len(calls) == 1
    assert calls[0][0] == "sts.aliyuncs.com"


@pytest.mark.asyncio
async def test_account_scoped_endpoint_uses_only_validated_identity(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    unavailable = tmp_path / "unavailable.json"
    overrides = tmp_path / "overrides.yml"
    catalog.write_text(
        json.dumps(
            {
                "_meta": {"default_versions": {}},
                "products": {
                    "SMQProxy": {
                        "2026-04-09": {
                            "regional_endpoints": {},
                            "global_endpoint": None,
                            "location_service_code": None,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    unavailable.write_text('{"products": []}', encoding="utf-8")
    overrides.write_text(
        """
trusted_endpoint_suffixes: [aliyuncs.com]
products:
  SMQProxy:
    "2026-04-09":
      account_id_host_template: "{account_id}.mns.{region_id}.aliyuncs.com"
""",
        encoding="utf-8",
    )

    class Identity:
        async def resolve(self, credential: Any, region_id: str) -> str:
            assert credential == "credential"
            assert region_id == "cn-hangzhou"
            return "1234567890123456"

    resolver = EndpointResolver(
        cache_dir=tmp_path / "cache",
        account_identity=Identity(),
        catalog_path=catalog,
        unavailable_path=unavailable,
        overrides_path=overrides,
    )
    result = await resolver.resolve(
        api_contract(product="SMQProxy", version="2026-04-09"),
        "cn-hangzhou",
        "credential",
    )

    assert result.source == "override"
    assert result.endpoint == "1234567890123456.mns.cn-hangzhou.aliyuncs.com"


@pytest.mark.asyncio
async def test_default_location_adapter_uses_fixed_host_and_filters_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def fake_request(host: str, request: Any) -> dict[str, Any]:
        calls.append((host, request))
        return {
            "Endpoints": {
                "Endpoint": [
                    {"Type": "internal", "Endpoint": "internal.aliyuncs.com"},
                    {"Type": "openAPI", "Endpoint": "ecs.cn-hangzhou.aliyuncs.com"},
                ]
            }
        }

    monkeypatch.setattr("iac_code.tools.cloud.aliyun.endpoint_resolver._call_location_api", fake_request)
    result = await LocationResolver().resolve("Ecs", "2014-05-26", "cn-hangzhou", "ecs", "credential")
    assert result == "ecs.cn-hangzhou.aliyuncs.com"
    assert calls == [
        (
            "location.aliyuncs.com",
            {
                "credential": "credential",
                "action": "DescribeEndpoints",
                "version": "2015-06-12",
                "region_id": "cn-hangzhou",
                "service_code": "ecs",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-hangzhou", "accessanalyzer.cn-hangzhou.aliyuncs.com"),
        ("cn-beijing", "accessanalyzer.cn-beijing.aliyuncs.com"),
        ("cn-shanghai", "accessanalyzer.cn-shanghai.aliyuncs.com"),
        ("ap-southeast-1", "accessanalyzer.ap-southeast-1.aliyuncs.com"),
    ],
)
async def test_access_analyzer_endpoint_override_uses_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AccessAnalyzer", version="2024-02-01", action="ListAnalyzers"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_access_analyzer_uses_reviewed_pattern_only_after_known_endpoint_sources_are_empty(
    tmp_path: Path,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AccessAnalyzer", version="2024-02-01", action="ListAnalyzers"),
        "cn-shenzhen",
        credential="credential",
    )

    assert result.source == "override_pattern"
    assert result.wire_endpoint == "accessanalyzer.cn-shenzhen.aliyuncs.com"


@pytest.mark.asyncio
async def test_explicit_endpoint_precedes_catalog_and_accepts_unknown_product_record(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Unknown", version="2026-07-19", action="ListThings"),
        "cn-shenzhen",
        credential="credential",
        explicit_endpoint="known.cn-shenzhen.aliyuncs.com",
    )

    assert result.source == "explicit"
    assert result.wire_endpoint == "known.cn-shenzhen.aliyuncs.com"


@pytest.mark.asyncio
async def test_explicit_endpoint_rejects_untrusted_hostname(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    with pytest.raises(EndpointResolutionError, match="^untrusted_endpoint$"):
        await resolver.resolve(
            api_contract(product="Unknown", version="2026-07-19", action="ListThings"),
            "cn-shenzhen",
            credential="credential",
            explicit_endpoint="example.com",
        )


@pytest.mark.asyncio
async def test_btrip_open_uses_documented_global_business_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="btripOpen", version="2022-05-20", action="VatInvoiceScanQuery"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "btripopen.alibtrip.com"


@pytest.mark.asyncio
async def test_catalog_public_region_endpoint_is_used_as_global_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AgentExplorer", version="2026-03-17", action="SearchSkills"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "agentexplorer.aliyuncs.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint", "source"),
    [
        ("cn-hangzhou", "resourcecenter.aliyuncs.com", "catalog_global"),
        ("cn-shanghai", "resourcecenter.aliyuncs.com", "catalog_region"),
        ("ap-southeast-1", "resourcecenter-intl.aliyuncs.com", "catalog_region"),
    ],
)
async def test_resource_center_uses_documented_central_endpoints(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
    source: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="ResourceCenter", version="2022-12-01", action="SearchResources"),
        region_id,
        credential="credential",
    )

    assert result.source == source
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-hangzhou", "agentteams.cn-hangzhou.aliyuncs.com"),
        ("cn-beijing", "agentteams.cn-beijing.aliyuncs.com"),
        ("ap-southeast-1", "agentteams.ap-southeast-1.aliyuncs.com"),
    ],
)
async def test_agentteams_endpoint_override_uses_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AgentTeams", version="2026-06-05", action="ListInstances"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-hangzhou", "agentloop.cn-hangzhou.aliyuncs.com"),
        ("cn-shanghai", "agentloop.cn-shanghai.aliyuncs.com"),
        ("cn-hongkong", "agentloop.cn-hongkong.aliyuncs.com"),
        ("ap-southeast-1", "agentloop.ap-southeast-1.aliyuncs.com"),
    ],
)
async def test_agentloop_endpoint_override_uses_supported_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AgentLoop", version="2026-05-20", action="DescribeRegions"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_aideepsign_endpoint_override_uses_hangzhou_regional_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AIDeepSign", version="2026-05-11", action="DetectAigcImage"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "aideepsign.cn-hangzhou.aliyuncs.com"


@pytest.mark.asyncio
async def test_aidge_endpoint_override_uses_beijing_regional_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Aidge", version="2026-04-28", action="QueryAsyncTaskResult"),
        "cn-beijing",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "aidge.cn-beijing.aliyuncs.com"


@pytest.mark.asyncio
async def test_bailian_voice_bot_endpoint_override_uses_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="BailianVoiceBot", version="2025-01-01", action="ListVoices"),
        "cn-beijing",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "bailianvoicebot.cn-beijing.aliyuncs.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-hangzhou", "airegistry.cn-hangzhou.aliyuncs.com"),
        ("cn-hongkong", "airegistry.cn-hongkong.aliyuncs.com"),
        ("ap-southeast-1", "airegistry.ap-southeast-1.aliyuncs.com"),
        ("eu-central-1", "airegistry.eu-central-1.aliyuncs.com"),
        ("us-east-1", "airegistry.us-east-1.aliyuncs.com"),
    ],
)
async def test_airegistry_endpoint_override_uses_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AIRegistry", version="2026-03-17", action="ListNamespaces"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-shanghai", "aisc.cn-shanghai.aliyuncs.com"),
        ("cn-beijing", "aisc.cn-shanghai.aliyuncs.com"),
        ("ap-southeast-1", "aisc.ap-southeast-1.aliyuncs.com"),
        ("eu-central-1", "aisc.ap-southeast-1.aliyuncs.com"),
    ],
)
async def test_aisc_endpoint_override_routes_supported_regions_to_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="AISC", version="2026-01-01", action="ListSubTasks"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_real_translation_agent_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="RealTranslationAgent", version="2026-06-22", action="ListTranslationTasks"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "realtranslationagent.aliyuncs.com"


@pytest.mark.asyncio
async def test_alikafka_kopilot_endpoint_override_uses_beijing_regional_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(
            product="AlikafkaKopilot",
            version="2026-04-14",
            action="KopilotListConversationChatMessages",
        ),
        "cn-beijing",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "alikafkakopilot.cn-beijing.aliyuncs.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-beijing", "starops.cn-beijing.aliyuncs.com"),
        ("ap-southeast-1", "starops.ap-southeast-1.aliyuncs.com"),
    ],
)
async def test_starops_endpoint_override_uses_supported_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="STAROps", version="2026-04-28", action="ListDigitalEmployees"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("region_id", ["cn-hangzhou", "cn-shanghai"])
async def test_starops_endpoint_override_rejects_known_unsupported_regions(
    tmp_path: Path,
    region_id: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    with pytest.raises(EndpointResolutionError, match="^endpoint_unavailable"):
        await resolver.resolve(
            api_contract(product="STAROps", version="2026-04-28", action="ListDigitalEmployees"),
            region_id,
            credential="credential",
        )


@pytest.mark.parametrize(
    ("product", "version", "action", "region_id", "source", "endpoint"),
    (
        (
            "MaasAISearchProxy",
            "2026-04-24",
            "WebSearch",
            "cn-hangzhou",
            "catalog_global",
            "maasaisearchproxy.aliyuncs.com",
        ),
        (
            "AiSearchEngine",
            "2026-04-17",
            "GetDatasetResourceUrl",
            "cn-hangzhou",
            "override",
            "aisearchengine.aliyuncs.com",
        ),
        (
            "ContactCenterAI",
            "2024-06-03",
            "GetVocab",
            "cn-shanghai",
            "catalog_region",
            "contactcenterai.cn-shanghai.aliyuncs.com",
        ),
        (
            "retailadvqa",
            "2023-04-17",
            "QueryMemberBasicInfo",
            "cn-shanghai",
            "catalog_region",
            "quicka.cn-shanghai.aliyuncs.com",
        ),
        (
            "SuperappNlp",
            "2024-09-30",
            "NlpAddressNormalization",
            "ap-southeast-1",
            "catalog_region",
            "superappnlp.ap-southeast-1.aliyuncs.com",
        ),
        (
            "tingwu",
            "2023-09-30",
            "ListTranscriptionPhrases",
            "cn-beijing",
            "catalog_region",
            "tingwu.cn-beijing.aliyuncs.com",
        ),
        (
            "safconsole",
            "2025-05-21",
            "DescribeModelingProjectList",
            "cn-shanghai",
            "override",
            "safconsole.cn-shanghai.aliyuncs.com",
        ),
        (
            "AgentRetailVision",
            "2026-05-06",
            "QueryRecognitionResult",
            "cn-beijing",
            "override",
            "agentretailvision.cn-beijing.aliyuncs.com",
        ),
        (
            "airticketOpen",
            "2023-01-17",
            "Search",
            "cn-hangzhou",
            "catalog_global",
            "airticketopen.aliyuncs.com",
        ),
        (
            "rtc-white-board",
            "2020-12-14",
            "DescribeApps",
            "cn-shanghai",
            "override",
            "rtc-white-board.cn-shanghai.aliyuncs.com",
        ),
        (
            "objectdet",
            "2019-12-30",
            "GetAsyncJobResult",
            "cn-shanghai",
            "override",
            "objectdet.cn-shanghai.aliyuncs.com",
        ),
        (
            "TrafficFxOpen",
            "2024-08-15",
            "Search",
            "cn-hangzhou",
            "catalog_global",
            "trafficfxopen.aliyuncs.com",
        ),
    ),
)
@pytest.mark.asyncio
async def test_live_validation_endpoint_overrides_use_official_openmeta_endpoints(
    tmp_path: Path,
    product: str,
    version: str,
    action: str,
    region_id: str,
    source: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product=product, version=version, action=action),
        region_id,
        credential="credential",
    )

    assert result.source == source
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-shanghai", "chatbot.cn-shanghai.aliyuncs.com"),
        ("cn-hongkong", "chatbot.cn-hongkong.aliyuncs.com"),
    ],
)
async def test_chatbot_2022_endpoint_override_uses_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Chatbot", version="2022-04-08", action="SearchDoc"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_risk_management_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="RiskManagement", version="2026-04-24", action="GetAliYunSafeCenterResult"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "riskmanagement.aliyuncs.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "endpoint"),
    [
        ("cn-hangzhou", "wyota.cn-hangzhou.aliyuncs.com"),
        ("ap-southeast-1", "wyota.ap-southeast-1.aliyuncs.com"),
        ("eu-west-1", "wyota.eu-west-1.aliyuncs.com"),
    ],
)
async def test_wyota_endpoint_override_uses_supported_regional_public_endpoint(
    tmp_path: Path,
    region_id: str,
    endpoint: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="wyota", version="2021-04-20", action="DescribeClients"),
        region_id,
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_grace_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="grace", version="2022-06-06", action="GetFile"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "grace.aliyuncs.com"


@pytest.mark.asyncio
async def test_dtsai_endpoint_override_uses_beijing_regional_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="DtsAI", version="2026-04-01", action="DescribeDocParserJobStatus"),
        "cn-beijing",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "dtsai.cn-beijing.aliyuncs.com"


@pytest.mark.asyncio
async def test_gemp_catalog_uses_shanghai_regional_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="GEMP", version="2021-04-13", action="ListConfigs"),
        "cn-shanghai",
        credential="credential",
    )

    assert result.source == "catalog_region"
    assert result.wire_endpoint == "gemp.cn-shanghai.aliyuncs.com"


@pytest.mark.asyncio
async def test_iot_legacy_endpoint_override_uses_official_shanghai_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Iot", version="2016-05-30", action="ListRule"),
        "cn-shanghai",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "iot.cn-shanghai.aliyuncs.com"


@pytest.mark.asyncio
async def test_smqproxy_uses_official_account_scoped_endpoint(tmp_path: Path) -> None:
    class Identity:
        async def resolve(self, credential: Any, region_id: str) -> str:
            assert credential == "credential"
            assert region_id == "cn-hangzhou"
            return "1234567890123456"

    resolver = EndpointResolver(
        cache_dir=tmp_path,
        location=FakeLocation([Exception("location should not be used")]),
        account_identity=Identity(),
    )

    result = await resolver.resolve(
        api_contract(product="SMQProxy", version="2026-04-09", action="PeekMessage"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "override"
    assert result.wire_endpoint == "1234567890123456.mns.cn-hangzhou.aliyuncs.com"


@pytest.mark.asyncio
async def test_osssddp_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="OssSddp", version="2024-02-22", action="GetSddpVersion"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "osssddp.aliyuncs.com"


@pytest.mark.asyncio
async def test_config_default_version_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Config", version="2020-09-07", action="ListConfigRules"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "config.aliyuncs.com"


@pytest.mark.asyncio
async def test_config_legacy_version_endpoint_override_uses_global_public_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    result = await resolver.resolve(
        api_contract(product="Config", version="2019-01-08", action="ListConfigRules"),
        "cn-hangzhou",
        credential="credential",
    )

    assert result.source == "catalog_global"
    assert result.wire_endpoint == "config.aliyuncs.com"


@pytest.mark.asyncio
async def test_sls_location_override_uses_lowercase_service_code_and_project_host(tmp_path: Path) -> None:
    host_parameter = ParameterMetadata(
        "project", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    location = FakeLocation(["cn-hangzhou.log.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)
    contract = api_contract(
        host_parameter,
        product="Sls",
        version="2020-12-30",
        action="GetLogsV2",
        style="ROA",
        method="POST",
        pathname="/logstores/{logstore}/logs",
    )

    result = await resolver.resolve(
        contract,
        "cn-hangzhou",
        credential="credential",
        host_values={"project": "ali-test-project"},
    )

    assert location.calls == [("Sls", "2020-12-30", "cn-hangzhou", "sls", "credential")]
    assert result.source == "location"
    assert result.endpoint == "cn-hangzhou.log.aliyuncs.com"
    assert result.host_template == "{project}.{endpoint}"
    assert (
        HostBindingResolver(("aliyuncs.com",)).bind(
            contract,
            result.endpoint,
            result.host_template,
            {"project": "ali-test-project"},
        )
        == "ali-test-project.cn-hangzhou.log.aliyuncs.com"
    )


@pytest.mark.asyncio
async def test_sls_project_level_api_does_not_inherit_project_host_template(tmp_path: Path) -> None:
    location = FakeLocation(["cn-hangzhou.log.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)
    contract = api_contract(
        product="Sls",
        version="2020-12-30",
        action="ListProject",
        style="ROA",
        method="GET",
        pathname="/",
    )

    result = await resolver.resolve(contract, "cn-hangzhou", credential="credential")

    assert location.calls == [("Sls", "2020-12-30", "cn-hangzhou", "sls", "credential")]
    assert result.source == "location"
    assert result.endpoint == "cn-hangzhou.log.aliyuncs.com"
    assert result.host_template is None
    assert HostBindingResolver(("aliyuncs.com",)).bind(contract, result.endpoint, result.host_template, {}) == (
        "cn-hangzhou.log.aliyuncs.com"
    )


def api_contract(*parameters: ParameterMetadata, **changes: Any) -> CanonicalWireContract:
    values: dict[str, Any] = {
        "metadata_source": "fresh",
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "method": "POST",
        "pathname": "/",
        "operation_type": "read",
        "auth_type": "AK",
        "signature_scheme": "acs3",
        "transport": "tea",
        "executable": True,
        "unsupported_reasons": (),
        "parameters": parameters,
        "consumes": (),
        "produces": (),
        "policy_digest": "fixture",
    }
    values.update(changes)
    return CanonicalWireContract(**values)


def generate(output: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/aliyun/generate_endpoints.py"),
            "--source",
            str(SOURCE),
            "--source-commit",
            COMMIT,
            "--products-sha256",
            SOURCE_SHA256,
            "--openmeta-products",
            str(OPENMETA_PRODUCTS),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )


def test_openmeta_product_snapshot_is_complete_unique_and_fully_audited() -> None:
    import scripts.aliyun.generate_endpoints as generator

    fixture = json.loads(OPENMETA_PRODUCTS.read_text(encoding="utf-8"))
    products = fixture["products"]
    assert fixture["source_url"] == OPENMETA_PRODUCTS_URL
    assert datetime.fromisoformat(fixture["fetched_at"]).tzinfo is not None
    assert len(products) == OPENMETA_PRODUCT_COUNT

    normalized = json.dumps(products, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert fixture["products_sha256"] == OPENMETA_PRODUCTS_SHA256 == hashlib.sha256(normalized).hexdigest()

    identities: list[str] = []
    valid_default_versions: dict[str, str] = {}
    for entry in products:
        assert isinstance(entry, dict) and set(entry) == {"product", "defaultVersion"}
        product = entry["product"]
        assert isinstance(product, str) and PRODUCT_IDENTIFIER.fullmatch(product)
        identity = product.casefold()
        identities.append(identity)
        version = entry["defaultVersion"]
        if isinstance(version, str) and SAFE_API_VERSION.fullmatch(version):
            valid_default_versions[identity] = version
    assert len(identities) == len(set(identities)), "OpenMeta product identities must be unique case-insensitively"

    catalog = json.loads((ENDPOINT_DATA / "catalog.json").read_text(encoding="utf-8"))
    unavailable = json.loads((ENDPOINT_DATA / "unavailable.json").read_text(encoding="utf-8"))["products"]
    report = json.loads((ENDPOINT_DATA / "generation_report.json").read_text(encoding="utf-8"))
    catalog_products = {product.casefold(): versions for product, versions in catalog["products"].items()}
    endpoint_overrides, _ = generator._load_overrides()
    _, override_products = generator._validate_overrides(endpoint_overrides)
    overrides = {product.casefold(): versions for product, versions in override_products.items()}
    unavailable_products = [entry["product"].casefold() for entry in unavailable]
    assert len(unavailable_products) == len(set(unavailable_products))

    available_products: set[str] = set()
    for product in valid_default_versions:
        catalog_versions = catalog_products.get(product, {})
        override_versions = overrides.get(product, {})
        for version in set(catalog_versions) | set(override_versions):
            effective = merge_endpoint_record(catalog_versions.get(version), override_versions.get(version))
            if (
                effective.location_service_code
                or effective.region_overrides
                or effective.regional
                or effective.global_endpoint
                or effective.account_id_host_template
            ):
                available_products.add(product)
                break
    assert set(identities) == available_products | set(unavailable_products)
    assert set(unavailable_products) == set(identities) - available_products
    assert {product.casefold(): version for product, version in catalog["_meta"]["default_versions"].items()} == (
        valid_default_versions
    )
    invalid_default_versions = set(identities) - set(valid_default_versions)
    for entry in unavailable:
        assert set(entry) == {"product", "checked_on", "reason"}
        assert entry["checked_on"] == fixture["fetched_at"].split("T", 1)[0]
        expected_reason = (
            "invalid_default_version"
            if entry["product"].casefold() in invalid_default_versions
            else "upstream_metadata_unavailable"
        )
        assert entry["reason"] == expected_reason

    assert report["openmeta_source_url"] == fixture["source_url"]
    assert report["openmeta_fetched_at"] == fixture["fetched_at"]
    assert report["openmeta_products_sha256"] == fixture["products_sha256"]
    assert report["openmeta_fixture_sha256"] == hashlib.sha256(OPENMETA_PRODUCTS.read_bytes()).hexdigest()
    assert report["counts"]["openmeta_products"] == len(identities)
    assert report["counts"]["available_products"] == len(available_products)
    assert report["counts"]["unavailable_products"] == len(unavailable_products)
    assert report["counts"]["openmeta_products_with_valid_default_version"] == len(valid_default_versions)
    assert report["counts"]["openmeta_products_without_valid_default_version"] == len(invalid_default_versions)


def test_generator_is_deterministic_and_matches_committed_outputs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate(first)
    generate(second)
    names = ("catalog.json", "unavailable.json", "generation_report.json")
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert (first / name).read_bytes() == (ENDPOINT_DATA / name).read_bytes()
    report = json.loads((first / "generation_report.json").read_text(encoding="utf-8"))
    assert report["source_commit"] == COMMIT
    assert report["source_repository"] == SOURCE_REPOSITORY
    assert report["source_path"] == "metadatas/products.json"
    assert report["source_sha256"] == SOURCE_SHA256 == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert (
        report["endpoint_overrides_sha256"]
        == hashlib.sha256((ENDPOINT_DATA / "overrides.yml").read_bytes()).hexdigest()
    )
    assert report["counts"]["source_records"] == 323


def test_generator_rejects_any_noncanonical_identity(tmp_path: Path) -> None:
    from scripts.aliyun.generate_endpoints import generate as generate_endpoints

    with pytest.raises(ValueError, match="pinned source commit mismatch"):
        generate_endpoints(
            source=SOURCE,
            source_commit="0" * 40,
            products_sha256=SOURCE_SHA256,
            openmeta_products=OPENMETA_PRODUCTS,
            output_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="pinned products SHA-256 mismatch"):
        generate_endpoints(
            source=SOURCE,
            source_commit=COMMIT,
            products_sha256="0" * 64,
            openmeta_products=OPENMETA_PRODUCTS,
            output_dir=tmp_path,
        )


def test_generator_rejects_self_consistent_but_truncated_openmeta_snapshot(tmp_path: Path) -> None:
    from scripts.aliyun.generate_endpoints import generate as generate_endpoints

    fixture = json.loads(OPENMETA_PRODUCTS.read_text(encoding="utf-8"))
    fixture["products"] = fixture["products"][:100]
    normalized = json.dumps(fixture["products"], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    fixture["products_sha256"] = hashlib.sha256(normalized).hexdigest()
    truncated = tmp_path / "openmeta-products.json"
    truncated.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="pinned OpenMeta products"):
        generate_endpoints(
            source=SOURCE,
            source_commit=COMMIT,
            products_sha256=SOURCE_SHA256,
            openmeta_products=truncated,
            output_dir=tmp_path / "output",
        )


def test_generator_rejects_case_insensitive_product_collisions() -> None:
    import scripts.aliyun.generate_endpoints as generator

    source = {
        "products": [
            {
                "code": "Ecs",
                "version": "2014-05-26",
                "regional_endpoints": {"cn-hangzhou": "ecs.cn-hangzhou.aliyuncs.com"},
            },
            {
                "code": "ecs",
                "version": "2014-05-26",
                "regional_endpoints": {"cn-hangzhou": "other.cn-hangzhou.aliyuncs.com"},
            },
        ]
    }
    with pytest.raises(ValueError, match="duplicate endpoint product"):
        generator._parse_products(source, ("aliyuncs.com",))

    overrides = {
        "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
        "products": {
            "Ecs": {"2014-05-26": {"global": "ecs.aliyuncs.com"}},
            "ecs": {"2014-05-26": {"global": "other.aliyuncs.com"}},
        },
    }
    with pytest.raises(ValueError, match="duplicate override product"):
        generator._validate_overrides(overrides)


def test_generator_promotes_public_regional_endpoint_to_global_endpoint() -> None:
    import scripts.aliyun.generate_endpoints as generator

    products, trimmed, rejected = generator._parse_products(
        {
            "products": [
                {
                    "code": "AgentExplorer",
                    "version": "2026-03-17",
                    "regional_endpoints": {
                        "public": "agentexplorer.aliyuncs.com",
                        "cn-hangzhou": "agentexplorer.cn-hangzhou.aliyuncs.com",
                    },
                    "global_endpoint": "",
                }
            ]
        },
        ("aliyuncs.com",),
    )

    record = products["AgentExplorer"]["2026-03-17"]
    assert record["global_endpoint"] == "agentexplorer.aliyuncs.com"
    assert record["regional_endpoints"] == {"cn-hangzhou": "agentexplorer.cn-hangzhou.aliyuncs.com"}
    assert trimmed == []
    assert rejected == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("regional_endpoints", [], "invalid endpoint mapping"),
        ("global_endpoint", 0, "invalid global endpoint"),
        ("location_service_code", False, "invalid Location service code"),
    ],
)
def test_generator_rejects_false_valued_malformed_endpoint_fields(field: str, value: Any, error: str) -> None:
    import scripts.aliyun.generate_endpoints as generator

    entry: dict[str, Any] = {"code": "Ecs", "version": "2014-05-26"}
    entry[field] = value

    with pytest.raises(ValueError, match=f"^{error}$"):
        generator._parse_products({"products": [entry]}, ("aliyuncs.com",))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"trusted_endpoint_suffixes": ["Aliyuncs.com"], "products": {}},
            "invalid trusted endpoint suffix",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {"Ecs": {"2014-05-26": {"regions": {"cn_hangzhou": "ecs.aliyuncs.com"}}}},
            },
            "invalid override region",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {"Ecs": {"2014-05-26": {"regions": {"cn-hangzhou": "https://ecs.aliyuncs.com"}}}},
            },
            "invalid override endpoint",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {"Ecs": {"2014-05-26": {"global": "ecs.aliyuncs.com/path"}}},
            },
            "invalid override endpoint",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {"FC": {"2023-03-30": {"location_service_code": "FC/unsafe"}}},
            },
            "invalid Location service code",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {"Oss": {"2019-05-17": {"host_template": "{bucket.__class__}.{endpoint}"}}},
            },
            "invalid host template",
        ),
        (
            {
                "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
                "products": {
                    "Ecs": {
                        "2014-05-26": {
                            "regional_endpoint_pattern": "ecs-{region_id}.aliyuncs.com",
                        }
                    }
                },
            },
            "invalid regional endpoint pattern",
        ),
    ],
    ids=[
        "suffix",
        "region",
        "regional-endpoint",
        "global-endpoint",
        "service-code",
        "host-template",
        "regional-endpoint-pattern",
    ],
)
def test_generator_validates_every_controlled_endpoint_override_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    error: str,
) -> None:
    import scripts.aliyun.generate_endpoints as generator

    monkeypatch.setattr(generator, "_load_overrides", lambda: (overrides, "0" * 64))

    with pytest.raises(ValueError, match=f"^{error}$"):
        generator.generate(
            source=SOURCE,
            source_commit=COMMIT,
            products_sha256=SOURCE_SHA256,
            openmeta_products=OPENMETA_PRODUCTS,
            output_dir=tmp_path,
        )


def test_generator_requires_endpoint_override_evidence() -> None:
    import scripts.aliyun.generate_endpoints as generator

    overrides = {
        "trusted_endpoint_suffixes": ["aliyuncs.com"],
        "products": {"Ecs": {"2014-05-26": {"regions": {"cn-hangzhou": "ecs.cn-hangzhou.aliyuncs.com"}}}},
    }

    with pytest.raises(ValueError, match="^missing override evidence$"):
        generator._validate_overrides(overrides)


def test_generator_allows_host_template_that_replaces_endpoint() -> None:
    import scripts.aliyun.generate_endpoints as generator

    overrides = {
        "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
        "products": {
            "pds": {
                "2022-03-01": {
                    "source": "official PDS SDK documentation",
                    "reason": "PDS domain APIs bind domain_id directly into the domain API host.",
                    "checked_on": "2026-07-17",
                    "location_service_code": None,
                    "regions": {},
                    "global": None,
                    "host_template": "{domain_id}.api.aliyunpds.com",
                }
            }
        },
    }

    _, products = generator._validate_overrides(overrides)

    assert products["pds"]["2022-03-01"]["host_template"] == "{domain_id}.api.aliyunpds.com"


@pytest.mark.parametrize(
    ("record", "error"),
    [
        (
            {"regions": {"cn-shanghai": "ecs.cn-shanghai.aliyuncs.com"}},
            "redundant override region",
        ),
        (
            {"location_service_code": "ecs", "global": None, "host_template": None},
            "redundant override record",
        ),
    ],
    ids=["duplicate-region", "no-data-change"],
)
def test_generator_rejects_redundant_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, Any],
    error: str,
) -> None:
    import scripts.aliyun.generate_endpoints as generator

    record.update(
        {
            "source": "official endpoint catalog",
            "reason": "Regression fixture for redundant override validation.",
            "checked_on": "2026-07-19",
        }
    )
    overrides = {
        "trusted_endpoint_suffixes": ["aliyuncs.com", "aliyunpds.com"],
        "products": {"Ecs": {"2014-05-26": record}},
    }
    monkeypatch.setattr(generator, "_load_overrides", lambda: (overrides, "0" * 64))

    with pytest.raises(ValueError, match=f"^{error}$"):
        generator.generate(
            source=SOURCE,
            source_commit=COMMIT,
            products_sha256=SOURCE_SHA256,
            openmeta_products=OPENMETA_PRODUCTS,
            output_dir=tmp_path,
        )


def test_merge_endpoint_record_allows_explicit_clearing() -> None:
    catalog = {
        "location_service_code": "ecs",
        "regional_endpoints": {"cn-hangzhou": "catalog.aliyuncs.com"},
        "global_endpoint": "global.aliyuncs.com",
        "host_template": None,
    }
    override = {
        "location_service_code": None,
        "regions": {"cn-hangzhou": None, "cn-shanghai": "override.aliyuncs.com"},
        "global": None,
        "host_template": "{bucket}.{endpoint}",
    }
    effective = merge_endpoint_record(catalog, override)
    assert effective.location_service_code is None
    assert effective.regional == MappingProxyType({})
    assert effective.region_overrides == MappingProxyType({"cn-shanghai": "override.aliyuncs.com"})
    assert effective.global_endpoint is None
    assert effective.regional_endpoint_pattern is None
    assert effective.blocked_pattern_regions == frozenset({"cn-hangzhou"})
    assert effective.host_template == "{bucket}.{endpoint}"


@pytest.mark.asyncio
async def test_record_without_reviewed_pattern_does_not_fall_back_from_region_samples(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))

    with pytest.raises(EndpointResolutionError, match="^endpoint_unavailable"):
        await resolver.resolve(
            api_contract(product="AISC", version="2026-01-01", action="ListSubTasks"),
            "cn-shenzhen",
            credential="credential",
        )


def test_merge_endpoint_record_never_infers_pattern_from_uniform_region_samples() -> None:
    regions = {
        "cn-hangzhou": "probe.cn-hangzhou.aliyuncs.com",
        "cn-shanghai": "probe.cn-shanghai.aliyuncs.com",
    }

    without_pattern = merge_endpoint_record(None, {"regions": regions})
    with_pattern = merge_endpoint_record(
        None,
        {
            "regions": regions,
            "regional_endpoint_pattern": "probe.{region_id}.aliyuncs.com",
        },
    )

    assert without_pattern.regional_endpoint_pattern is None
    assert with_pattern.regional_endpoint_pattern == "probe.{region_id}.aliyuncs.com"


@pytest.mark.asyncio
async def test_yike_override_removes_invalid_hangzhou_and_adds_official_singapore_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([]))
    contract = api_contract(product="Yike", version="2026-03-19", action="ListYikeWorkspaces")

    resolved = await resolver.resolve(contract, "cn-shanghai", object())

    assert (resolved.endpoint, resolved.source) == ("yike.cn-shanghai.aliyuncs.com", "catalog_region")
    singapore = await resolver.resolve(contract, "ap-southeast-1", object())
    assert (singapore.endpoint, singapore.source) == ("yike.ap-southeast-1.aliyuncs.com", "override")
    with pytest.raises(EndpointResolutionError, match="endpoint_unavailable"):
        await resolver.resolve(contract, "cn-hangzhou", object())


@pytest.mark.asyncio
async def test_endpoint_resolution_uses_location_then_catalog_and_fc_v3(tmp_path: Path) -> None:
    location = FakeLocation(["ecs.cn-shanghai.aliyuncs.com", "ecs.cn-beijing.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)
    ecs = await resolver.resolve(api_contract(), "cn-shanghai", object())
    assert (ecs.endpoint, ecs.source) == ("ecs.cn-shanghai.aliyuncs.com", "location")

    fc = await resolver.resolve(
        api_contract(product="FC", version="2023-03-30", action="GetFunction"), "cn-hangzhou", object()
    )
    assert (fc.endpoint, fc.source) == ("fcv3.cn-hangzhou.aliyuncs.com", "catalog_region")

    location_result = await resolver.resolve(api_contract(), "cn-beijing", "credential")
    assert (location_result.endpoint, location_result.source) == ("ecs.cn-beijing.aliyuncs.com", "location")
    assert [call[0:4] for call in location.calls] == [
        ("Ecs", "2014-05-26", "cn-shanghai", "ecs"),
        ("Ecs", "2014-05-26", "cn-beijing", "ecs"),
    ]


@pytest.mark.asyncio
async def test_explicit_region_override_precedes_location_and_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    unavailable = tmp_path / "unavailable.json"
    overrides = tmp_path / "overrides.yml"
    catalog.write_text(
        json.dumps(
            {
                "_meta": {"default_versions": {}},
                "products": {
                    "Probe": {
                        "2026-07-19": {
                            "regional_endpoints": {"cn-shanghai": "probe.cn-shanghai.aliyuncs.com"},
                            "global_endpoint": None,
                            "location_service_code": "probe",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    unavailable.write_text('{"products": []}', encoding="utf-8")
    overrides.write_text(
        """
trusted_endpoint_suffixes: [aliyuncs.com]
products:
  Probe:
    "2026-07-19":
      regions:
        cn-shanghai: probe-override.cn-shanghai.aliyuncs.com
""".lstrip(),
        encoding="utf-8",
    )
    location = FakeLocation(["probe-location.cn-shanghai.aliyuncs.com"])
    resolver = EndpointResolver(
        cache_dir=tmp_path / "cache",
        location=location,
        catalog_path=catalog,
        unavailable_path=unavailable,
        overrides_path=overrides,
    )

    resolved = await resolver.resolve(
        api_contract(product="Probe", version="2026-07-19", action="Read"),
        "cn-shanghai",
        object(),
    )

    assert (resolved.endpoint, resolved.source) == (
        "probe-override.cn-shanghai.aliyuncs.com",
        "override",
    )
    assert location.calls == []


@pytest.mark.asyncio
async def test_location_uses_catalog_service_code_when_it_differs_from_product(tmp_path: Path) -> None:
    location = FakeLocation(["sae.cn-hangzhou.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)

    result = await resolver.resolve(
        api_contract(product="sae", version="2019-05-06"),
        "cn-hangzhou",
        "credential",
    )

    assert (result.endpoint, result.source) == ("sae.cn-hangzhou.aliyuncs.com", "location")
    assert location.calls == [("sae", "2019-05-06", "cn-hangzhou", "serverless", "credential")]


@pytest.mark.asyncio
async def test_empty_location_service_code_skips_location_and_uses_catalog(tmp_path: Path) -> None:
    location = FakeLocation([RuntimeError("Location must not run")])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)

    result = await resolver.resolve(
        api_contract(product="ocr-api", version="2021-07-07"),
        "cn-hangzhou",
        "credential",
    )

    assert (result.endpoint, result.source) == ("ocr-api.cn-hangzhou.aliyuncs.com", "catalog_region")
    assert location.calls == []


@pytest.mark.asyncio
async def test_endpoint_resolution_falls_back_regional_then_global_and_stable_error(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([None, None]))
    regional = await resolver.resolve(api_contract(), "cn-hangzhou", object())
    assert (regional.endpoint, regional.source) == ("ecs-cn-hangzhou.aliyuncs.com", "catalog_region")
    global_result = await resolver.resolve(api_contract(product="ROS", version="2019-09-10"), "cn-test", object())
    assert (global_result.endpoint, global_result.source) == ("ros.aliyuncs.com", "catalog_global")
    with pytest.raises(EndpointResolutionError, match="endpoint_unavailable"):
        await resolver.resolve(api_contract(product="Unknown", version="2020-01-01"), "cn-hangzhou", object())
    with pytest.raises(EndpointResolutionError, match="endpoint_unavailable:upstream_metadata_unavailable"):
        await resolver.resolve(api_contract(product="AliGenie", version="iap_1.0"), "cn-hangzhou", object())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region_id", "expected", "source"),
    [
        ("cn-beijing", "aicontent.cn-beijing.aliyuncs.com", "override"),
        ("cn-hangzhou", "aicontent.cn-hangzhou.aliyuncs.com", "override"),
        ("cn-shanghai", "aicontent.aliyuncs.com", "override"),
        ("cn-test", "aicontent.aliyuncs.com", "catalog_global"),
    ],
)
async def test_aicontent_compact_version_uses_official_endpoints(
    tmp_path: Path,
    region_id: str,
    expected: str,
    source: str,
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([]))

    result = await resolver.resolve(
        api_contract(product="AiContent", version="20240611"),
        region_id,
        object(),
    )

    assert (result.endpoint, result.source) == (expected, source)


@pytest.mark.asyncio
async def test_location_cache_key_ttls_and_revalidates_hostname_policy(tmp_path: Path) -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    clock_value = [now]
    location = FakeLocation(["fresh.aliyuncs.com", RuntimeError("offline"), None, "new.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location, clock=lambda: clock_value[0])
    contract = api_contract()
    first = await resolver.resolve(contract, "cn-beijing", object())
    assert first.endpoint == "fresh.aliyuncs.com"
    assert (await resolver.resolve(contract, "cn-beijing", object())).endpoint == "fresh.aliyuncs.com"
    assert len(location.calls) == 1

    clock_value[0] += timedelta(days=8)
    assert (await resolver.resolve(contract, "cn-beijing", object())).endpoint == "fresh.aliyuncs.com"
    assert len(location.calls) == 2

    empty_contract = replace(contract, version="2016-04-28")
    await resolver.resolve(empty_contract, "cn-hangzhou", object())
    await resolver.resolve(empty_contract, "cn-hangzhou", object())
    assert len(location.calls) == 3
    clock_value[0] += timedelta(minutes=11)
    await resolver.resolve(empty_contract, "cn-hangzhou", object())
    assert len(location.calls) == 4

    resolver._trusted_suffixes = ("aliyunpds.com",)
    with pytest.raises(EndpointResolutionError, match="untrusted_endpoint"):
        await resolver.resolve(contract, "cn-beijing", object())


@pytest.mark.asyncio
async def test_invalid_host_value_fails_before_location_or_credential_use(tmp_path: Path) -> None:
    host_parameter = ParameterMetadata(
        "bucket", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    location = FakeLocation(["location.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)
    credential = object()
    with pytest.raises(EndpointResolutionError, match="invalid_host_label"):
        await resolver.resolve(
            api_contract(host_parameter, product="Oss", version="2019-05-17", transport="oss_v4_sdk"),
            "cn-beijing",
            credential,
            host_values={"bucket": "bad.example"},
        )
    assert location.calls == []


@pytest.mark.asyncio
async def test_request_preflight_blocks_invalid_host_before_all_network_stages(tmp_path: Path) -> None:
    host_parameter = ParameterMetadata(
        "bucket", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    contract = api_contract(host_parameter)
    location = FakeLocation(["ecs.cn-test.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)
    stage_calls = {"credential": 0, "sdk": 0}

    async def run(tool_input: dict[str, Any]) -> Any:
        built = await RequestBuilder().build(contract, tool_input)
        stage_calls["credential"] += 1
        resolution = await resolver.resolve(
            contract,
            "cn-test",
            "credential",
            host_values=built.host_values,
        )
        stage_calls["sdk"] += 1
        return built, resolution

    with pytest.raises(ApiContractError, match="invalid_host_label"):
        await run({"params": {"bucket": "bad.example"}})
    assert stage_calls == {"credential": 0, "sdk": 0}
    assert location.calls == []

    built, resolution = await run({"params": {"bucket": "Demo-Bucket"}})
    assert built.host_values == {"bucket": "demo-bucket"}
    assert resolution.endpoint == "ecs.cn-test.aliyuncs.com"
    assert stage_calls == {"credential": 1, "sdk": 1}
    assert len(location.calls) == 1


@pytest.mark.asyncio
async def test_location_disk_cache_integrity_round_trip_and_corruption(tmp_path: Path) -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_location = FakeLocation(["fresh.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=first_location, clock=lambda: now)
    assert (await resolver.resolve(api_contract(), "cn-beijing", object())).endpoint == "fresh.aliyuncs.com"
    cache_path = tmp_path / "endpoints/location.json"
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(envelope) == {"schema_version", "fetched_at", "source_url", "payload_sha256", "payload"}
    payload_bytes = json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode("ascii")
    assert envelope["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()

    disk_hit_location = FakeLocation([RuntimeError("must not run")])
    disk_hit = EndpointResolver(cache_dir=tmp_path, location=disk_hit_location, clock=lambda: now)
    assert (await disk_hit.resolve(api_contract(), "cn-beijing", object())).endpoint == "fresh.aliyuncs.com"
    assert disk_hit_location.calls == []

    envelope["payload_sha256"] = "0" * 64
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")
    refresh_location = FakeLocation(["refreshed.aliyuncs.com"])
    refreshed = EndpointResolver(cache_dir=tmp_path, location=refresh_location, clock=lambda: now)
    assert (await refreshed.resolve(api_contract(), "cn-beijing", object())).endpoint == "refreshed.aliyuncs.com"
    assert len(refresh_location.calls) == 1

    valid_envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    corruptions = (
        {**valid_envelope, "schema_version": 99},
        {**valid_envelope, "fetched_at": 123},
        {**valid_envelope, "fetched_at": "not-a-date"},
        {**valid_envelope, "source_url": "https://evil.test/"},
        {**valid_envelope, "payload": []},
    )
    for index, corrupted in enumerate(corruptions):
        cache_path.write_text(json.dumps(corrupted), encoding="utf-8")
        location = FakeLocation([f"refresh-{index}.aliyuncs.com"])
        resolver = EndpointResolver(cache_dir=tmp_path, location=location, clock=lambda: now)
        assert (await resolver.resolve(api_contract(), "cn-beijing", object())).endpoint.startswith("refresh-")
        assert len(location.calls) == 1


@pytest.mark.asyncio
async def test_location_disk_failure_does_not_mutate_memory(tmp_path: Path) -> None:
    def fail_write(path: Path, document: Any) -> None:
        raise OSError("disk full")

    location = FakeLocation(["fresh.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location, cache_writer=fail_write)
    with pytest.raises(EndpointResolutionError, match="location_cache_write_failed"):
        await resolver.resolve(api_contract(), "cn-beijing", object())
    assert resolver._location_cache == {}


def test_atomic_location_cache_fsyncs_parent_after_replace(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "location.json"
    synced: list[Path] = []
    monkeypatch.setattr(endpoint_resolver_module, "fsync_parent_dir", synced.append, raising=False)

    endpoint_resolver_module._atomic_json(cache_path, {"value": "new"})

    assert json.loads(cache_path.read_text(encoding="ascii")) == {"value": "new"}
    assert synced == [cache_path]


@pytest.mark.parametrize("replace_before_failure", [False, True])
def test_atomic_location_cache_failure_leaves_complete_old_or_new_value(
    tmp_path: Path,
    monkeypatch,
    replace_before_failure: bool,
) -> None:
    cache_path = tmp_path / "location.json"
    endpoint_resolver_module._atomic_json(cache_path, {"value": "old"})
    real_replace = endpoint_resolver_module.os.replace

    def fail_replace(source: str, target: Path) -> None:
        if replace_before_failure:
            real_replace(source, target)
        raise OSError("simulated replace failure")

    monkeypatch.setattr(endpoint_resolver_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        endpoint_resolver_module._atomic_json(cache_path, {"value": "new"})

    assert json.loads(cache_path.read_text(encoding="ascii")) in ({"value": "old"}, {"value": "new"})


@pytest.mark.asyncio
async def test_endpoint_version_falls_back_to_openmeta_default_then_unique_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    unavailable = tmp_path / "unavailable.json"
    overrides = tmp_path / "overrides.yml"
    catalog.write_text(
        json.dumps(
            {
                "_meta": {"default_versions": {"Multi": "2020-01-01"}},
                "products": {
                    "Multi": {
                        "2020-01-01": {"regional_endpoints": {}, "global_endpoint": "multi.aliyuncs.com"},
                        "2021-01-01": {"regional_endpoints": {}, "global_endpoint": "other.aliyuncs.com"},
                    },
                    "Unique": {"2022-01-01": {"regional_endpoints": {}, "global_endpoint": "unique.aliyuncs.com"}},
                },
            }
        ),
        encoding="utf-8",
    )
    unavailable.write_text('{"products": []}', encoding="utf-8")
    overrides.write_text("trusted_endpoint_suffixes: [aliyuncs.com]\nproducts: {}\n", encoding="utf-8")
    resolver = EndpointResolver(
        cache_dir=tmp_path / "cache",
        catalog_path=catalog,
        unavailable_path=unavailable,
        overrides_path=overrides,
    )
    default = await resolver.resolve(api_contract(product="Multi", version="2099-01-01"), "cn-hangzhou", object())
    unique = await resolver.resolve(api_contract(product="Unique", version="2099-01-01"), "cn-hangzhou", object())
    assert default.endpoint == "multi.aliyuncs.com"
    assert unique.endpoint == "unique.aliyuncs.com"


def test_host_binding_uses_declared_single_labels_and_exact_template() -> None:
    host_parameter = ParameterMetadata(
        "bucket", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    resolver = HostBindingResolver(("aliyuncs.com",))
    contract = api_contract(host_parameter, product="Oss", version="2019-05-17", transport="oss_v4_sdk")
    assert (
        resolver.bind(contract, "oss-cn-hangzhou.aliyuncs.com", "{bucket}.{endpoint}", {"bucket": "demo-bucket"})
        == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"
    )
    for value in ("a.b", "a/b", "a:443", "a\n", "a%2eb", ""):
        with pytest.raises(EndpointResolutionError, match="invalid_host_label"):
            resolver.bind(contract, "oss-cn-hangzhou.aliyuncs.com", "{bucket}.{endpoint}", {"bucket": value})
    with pytest.raises(EndpointResolutionError, match="host_template_required"):
        resolver.bind(contract, "oss-cn-hangzhou.aliyuncs.com", None, {"bucket": "demo"})
    with pytest.raises(EndpointResolutionError, match="invalid_host_template"):
        resolver.bind(contract, "oss-cn-hangzhou.aliyuncs.com", "{other}.{endpoint}", {"bucket": "demo"})
    with pytest.raises(EndpointResolutionError, match="invalid_host_template"):
        resolver.bind(contract, "oss-cn-hangzhou.aliyuncs.com", "{bucket!r}.{endpoint}", {"bucket": "demo"})


def test_host_binding_allows_template_that_replaces_endpoint() -> None:
    host_parameter = ParameterMetadata(
        "domain_id", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    resolver = HostBindingResolver(("aliyunpds.com",))
    contract = api_contract(host_parameter, product="pds", version="2022-03-01", style="ROA")

    assert (
        resolver.bind(
            contract,
            "cn-hangzhou.admin.aliyunpds.com",
            "{domain_id}.api.aliyunpds.com",
            {"domain_id": "bj123"},
        )
        == "bj123.api.aliyunpds.com"
    )


def test_host_binding_rejects_missing_optional_value_required_by_template() -> None:
    host_parameter = ParameterMetadata(
        "domain_id", "host", False, None, None, MappingProxyType({"type": "string"}), None, None
    )
    resolver = HostBindingResolver(("aliyunpds.com",))
    contract = api_contract(host_parameter, product="pds", version="2022-03-01", style="ROA")

    with pytest.raises(EndpointResolutionError, match="missing_host_parameter"):
        resolver.bind(
            contract,
            "cn-hangzhou.admin.aliyunpds.com",
            "{domain_id}.api.aliyunpds.com",
            {},
        )


@pytest.mark.asyncio
async def test_pds_domain_host_binding_uses_official_domain_api_endpoint(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))
    domain_parameter = ParameterMetadata(
        "domain_id", "host", False, None, None, MappingProxyType({"type": "string"}), None, None
    )
    domain_contract = api_contract(
        domain_parameter,
        product="pds",
        version="2022-03-01",
        action="SearchUser",
        style="ROA",
        method="POST",
        pathname="/v2/user/search",
    )
    admin_contract = api_contract(
        product="pds",
        version="2022-03-01",
        action="ListDomains",
        style="ROA",
        method="POST",
        pathname="/v2/domain/list",
    )

    domain_endpoint = await resolver.resolve(
        domain_contract,
        "cn-hangzhou",
        credential="credential",
        host_values={"domain_id": "bj123"},
    )
    admin_endpoint = await resolver.resolve(admin_contract, "cn-hangzhou", credential="credential")

    assert domain_endpoint.source == "catalog_region"
    assert domain_endpoint.endpoint == "cn-hangzhou.admin.aliyunpds.com"
    assert domain_endpoint.host_template == "{domain_id}.api.aliyunpds.com"
    assert (
        resolver.host_binding_resolver.bind(
            domain_contract,
            domain_endpoint.endpoint,
            domain_endpoint.host_template,
            {"domain_id": "bj123"},
        )
        == "bj123.api.aliyunpds.com"
    )
    assert admin_endpoint.source == "catalog_region"
    assert admin_endpoint.wire_endpoint == "cn-hangzhou.admin.aliyunpds.com"
    assert admin_endpoint.host_template is None


@pytest.mark.asyncio
async def test_hcs_mgw_endpoint_uses_official_userid_host_binding(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([Exception("location should not be used")]))
    userid_parameter = ParameterMetadata(
        "userid", "host", True, None, None, MappingProxyType({"type": "string"}), None, None
    )
    contract = api_contract(
        userid_parameter,
        product="hcs-mgw",
        version="2024-06-26",
        action="ListJob",
        style="ROA",
        method="GET",
        pathname="/joblist",
    )

    endpoint = await resolver.resolve(
        contract,
        "cn-hangzhou",
        credential="credential",
        host_values={"userid": "xx"},
    )

    assert endpoint.endpoint == "cn-hangzhou.mgw.aliyuncs.com"
    assert endpoint.host_template == "{userid}.{endpoint}"
    assert (
        resolver.host_binding_resolver.bind(
            contract,
            endpoint.endpoint,
            endpoint.host_template,
            {"userid": "xx"},
        )
        == "xx.cn-hangzhou.mgw.aliyuncs.com"
    )


@pytest.mark.asyncio
async def test_single_runtime_factory_owns_task_3_services(tmp_path: Path) -> None:
    runtime = create_aliyun_runtime_services(cache_dir=tmp_path)
    try:
        assert runtime.contract_resolver._openmeta is runtime.openmeta
        assert runtime.request_builder is not None
        assert runtime.endpoint_resolver is not None
        assert runtime.host_binding_resolver is not None
    finally:
        await runtime.aclose()


def test_discovery_config_uses_the_dynamic_client_for_ecs_ram_role(fake_ecs_runtime: FakeEcsRuntime) -> None:
    values = endpoint_resolver_module._discovery_config_values(
        "location.aliyuncs.com", fake_ecs_runtime.credential(), "cn-hangzhou"
    )

    assert values["endpoint"] == "location.aliyuncs.com"
    assert values["region_id"] == "cn-hangzhou"
    assert type(values["credential"].cloud_credential.provider).__name__ == "EcsRamRoleProviderAdapter"
    # Endpoint and identity discovery must not fall back to an empty static AccessKey.
    assert "access_key_id" not in values
    assert "access_key_secret" not in values
    assert "security_token" not in values


def test_discovery_config_keeps_static_values_for_access_key_mode(fake_ecs_runtime: FakeEcsRuntime) -> None:
    from iac_code.services.providers.aliyun import AliyunCredential

    values = endpoint_resolver_module._discovery_config_values(
        "location.aliyuncs.com",
        AliyunCredential(mode="AK", access_key_id="fake-ak", access_key_secret="fake-secret"),
        "cn-hangzhou",
    )

    assert "credential" not in values
    assert (values["access_key_id"], values["access_key_secret"]) == ("fake-ak", "fake-secret")
    assert fake_ecs_runtime.providers == []


def test_location_and_identity_discovery_share_one_ecs_provider(fake_ecs_runtime: FakeEcsRuntime) -> None:
    credential = fake_ecs_runtime.credential()
    location = endpoint_resolver_module._discovery_config_values("location.aliyuncs.com", credential, "cn-hangzhou")
    identity = endpoint_resolver_module._discovery_config_values("sts.aliyuncs.com", credential, "cn-hangzhou")

    assert location["credential"].cloud_credential.provider is identity["credential"].cloud_credential.provider
    assert len(fake_ecs_runtime.providers) == 1


def test_discovery_config_reports_metadata_disabled_before_any_request(
    fake_ecs_runtime: FakeEcsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iac_code.services.providers.aliyun_credentials_runtime import ECS_METADATA_DISABLED

    monkeypatch.setenv("ALIBABA_CLOUD_ECS_METADATA_DISABLED", "true")

    with pytest.raises(ValueError) as raised:
        endpoint_resolver_module._discovery_config_values(
            "location.aliyuncs.com", fake_ecs_runtime.credential(), "cn-hangzhou"
        )

    assert str(raised.value) == ECS_METADATA_DISABLED
    assert fake_ecs_runtime.providers == []
