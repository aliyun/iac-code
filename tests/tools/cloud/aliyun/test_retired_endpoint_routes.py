from __future__ import annotations

from pathlib import Path

import pytest

from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolutionError, EndpointResolver
from tests.tools.cloud.aliyun.test_endpoint_resolver import FakeLocation, api_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["ap-south-1", "ap-southeast-2"])
@pytest.mark.parametrize(("product", "version"), [("Ecs", "2014-05-26"), ("Oss", "2019-05-17")])
async def test_retired_region_cannot_select_regional_or_discovered_host(
    tmp_path: Path, product: str, version: str, region: str
) -> None:
    location = FakeLocation([f"service.{region}.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)

    with pytest.raises(EndpointResolutionError, match="^endpoint_unavailable"):
        await resolver.resolve(api_contract(product=product, version=version), region, "fake-credential")

    assert location.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["ap-south-1", "ap-southeast-2"])
async def test_retired_client_region_can_use_existing_global_service_endpoint(tmp_path: Path, region: str) -> None:
    location = FakeLocation(["regional.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)

    result = await resolver.resolve(api_contract(product="Sts", version="2015-04-01"), region, "fake-credential")

    assert result.endpoint == "sts.aliyuncs.com"
    assert location.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("product", "version"), [("Ddi", "2020-06-17"), ("openanalytics-open", "2018-06-19")])
async def test_retired_product_does_not_use_old_catalog_or_location_routes(
    tmp_path: Path, product: str, version: str
) -> None:
    location = FakeLocation(["old.aliyuncs.com"])
    resolver = EndpointResolver(cache_dir=tmp_path, location=location)

    with pytest.raises(EndpointResolutionError, match="^endpoint_unavailable:service_retired$"):
        await resolver.resolve(api_contract(product=product, version=version), "cn-hangzhou", "fake-credential")

    assert location.calls == []


@pytest.mark.asyncio
async def test_explicit_trusted_endpoint_remains_available_for_retired_routing(tmp_path: Path) -> None:
    resolver = EndpointResolver(cache_dir=tmp_path, location=FakeLocation([]))

    result = await resolver.resolve(
        api_contract(product="Ddi", version="2020-06-17"),
        "ap-south-1",
        None,
        explicit_endpoint="custom.aliyuncs.com",
    )

    assert result.endpoint == "custom.aliyuncs.com"
    assert result.source == "explicit"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("unavailable_regions", [], "invalid unavailable region"),
        ("unavailable_regions", {"cn-test.": {}}, "invalid unavailable region"),
        ("unavailable_regions", {"cn-test": {}}, "missing override evidence"),
        ("retired_products", [], "invalid retired product"),
        ("retired_products", {"bad/product": {}}, "invalid retired product"),
        ("retired_products", {"Ddi": {}}, "missing override evidence"),
    ],
)
def test_generator_rejects_retirement_policies_without_valid_identity_or_evidence(
    field: str, value: object, error: str
) -> None:
    from scripts.aliyun.generate_endpoints import _validate_overrides

    with pytest.raises(ValueError, match=error):
        _validate_overrides({"trusted_endpoint_suffixes": ["aliyuncs.com"], field: value})
