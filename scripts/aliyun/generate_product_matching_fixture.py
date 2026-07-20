#!/usr/bin/env python3
"""Generate the compact, offline Alibaba Cloud product matching fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.aliyun.openmeta import load_openmeta_exclusions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--exclusions", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.input.read_text(encoding="utf-8"))
    payload = envelope.get("payload")
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        raise ValueError("input is not an OpenMeta products cache envelope")
    exclusions = load_openmeta_exclusions(args.exclusions)

    compact_products: list[dict[str, Any]] = []
    excluded_products: list[str] = []
    for raw in products:
        if not isinstance(raw, dict) or not isinstance(raw.get("code"), str):
            raise ValueError("input contains an invalid product record")
        code = raw["code"]
        short_name = raw.get("shortName")
        if short_name is not None and not isinstance(short_name, str):
            raise ValueError("input contains an invalid product shortName")
        compact_products.append({"code": code, "shortName": short_name})
        if exclusions.product_excluded(code):
            excluded_products.append(code)

    compact_products.sort(key=lambda item: item["code"].casefold())
    encoded_products = json.dumps(
        compact_products,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    fixture = {
        "_meta": {
            "schema_version": 1,
            "source_url": envelope.get("source_url"),
            "source_fetched_at": envelope.get("fetched_at"),
            "source_payload_sha256": envelope.get("payload_sha256"),
            "fixture_products_sha256": hashlib.sha256(encoded_products).hexdigest(),
        },
        "excluded_products": sorted(excluded_products, key=str.casefold),
        "products": compact_products,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
