#!/usr/bin/env python3
"""Generate the bundled Alibaba Cloud Product recognition catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.aliyun.openmeta import ProductMetadata
from iac_code.tools.cloud.aliyun.product_resolver import normalize_catalog_short_name, product_catalog_digest

_DEFAULT_SOURCE_URL = "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path, help="official products.json or a cache envelope")
    parser.add_argument("--output", required=True, type=Path, help="generated bundled product_catalog.json")
    parser.add_argument("--source-url", default=_DEFAULT_SOURCE_URL, help="provenance URL for a raw products.json")
    parser.add_argument("--source-fetched-at", help="ISO-8601 fetch time for a raw products.json")
    args = parser.parse_args()

    input_bytes = args.input.read_bytes()
    source = json.loads(input_bytes)
    if isinstance(source, list):
        raw_products = source
        source_url = args.source_url
        source_fetched_at = args.source_fetched_at or datetime.now(timezone.utc).isoformat()
        source_payload_sha256 = hashlib.sha256(input_bytes).hexdigest()
    elif isinstance(source, dict):
        payload = source.get("payload")
        raw_products = payload.get("products") if isinstance(payload, dict) else None
        source_url = source.get("source_url")
        source_fetched_at = source.get("fetched_at")
        source_payload_sha256 = source.get("payload_sha256")
    else:
        raw_products = None
        source_url = source_fetched_at = source_payload_sha256 = None
    if not isinstance(raw_products, list):
        raise ValueError("input is neither an official products.json array nor an OpenMeta cache envelope")
    if any(not isinstance(value, str) or not value for value in (source_url, source_fetched_at, source_payload_sha256)):
        raise ValueError("input has incomplete source provenance")

    products: list[dict[str, Any]] = []
    for raw in raw_products:
        if not isinstance(raw, dict):
            raise ValueError("input contains an invalid product record")
        metadata = ProductMetadata.from_openmeta(raw)
        products.append(
            {
                "code": metadata.product,
                "defaultVersion": metadata.default_version,
                "recommendVersions": list(metadata.recommended_versions),
                "shortName": normalize_catalog_short_name(raw.get("shortName")),
                "style": metadata.style,
                "versions": list(metadata.versions),
            }
        )

    products.sort(key=lambda item: item["code"].casefold())
    catalog = {
        "_meta": {
            "schema_version": 1,
            "source_url": source_url,
            "source_fetched_at": source_fetched_at,
            "source_payload_sha256": source_payload_sha256,
            "catalog_products_sha256": product_catalog_digest(products),
        },
        "products": products,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
