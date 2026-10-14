"""Exercise production endpoint routing against independently reviewed public sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolver
from tests.tools.cloud.aliyun.test_endpoint_resolver import FakeLocation, api_contract

_REVIEW = json.loads(
    (Path(__file__).parent / "fixtures/endpoints/reviewed_public_endpoints.json").read_text(encoding="utf-8")
)
_REGIONAL_CASES = [
    pytest.param(record["product"], record["version"], region, endpoint, id=f"{record['product']}-{region}")
    for record in _REVIEW["records"]
    for region, endpoint in record["regions"].items()
]
_CENTRAL_CASES = [
    pytest.param(record["product"], record["version"], record["global"], id=f"{record['product']}-{record['version']}")
    for record in _REVIEW["records"]
    if record["global"] is not None
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("product", "version", "region", "endpoint"), _REGIONAL_CASES)
async def test_reviewed_public_region_resolves_without_location_discovery(
    tmp_path: Path, product: str, version: str, region: str, endpoint: str
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([RuntimeError("Location unavailable")]))

    result = await resolver.resolve(api_contract(product=product, version=version), region, None)

    assert result.endpoint == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(("product", "version", "endpoint"), _CENTRAL_CASES)
async def test_reviewed_central_service_resolves_from_client_region(
    tmp_path: Path, product: str, version: str, endpoint: str
) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([RuntimeError("Location unavailable")]))

    result = await resolver.resolve(api_contract(product=product, version=version), "cn-hangzhou", None)

    assert result.endpoint == endpoint
