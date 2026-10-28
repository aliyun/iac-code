"""Unambiguous payable prices for model-facing pipeline Steps."""

from typing import Any

from iac_code.tools.cloud.aliyun.api_hooks import step_result_projector


@step_result_projector("ecs", "DescribePrice")
def project_ecs_price(params: dict[str, Any], body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    price_info = body.get("PriceInfo")
    if not isinstance(price_info, dict) or not isinstance(price_info.get("Price"), dict):
        return body
    price = price_info["Price"]
    detail_infos = price.get("DetailInfos")
    details = detail_infos.get("DetailInfo", []) if isinstance(detail_infos, dict) else []
    return {
        "Currency": price.get("Currency"),
        "PriceUnit": params.get("PriceUnit", "Hour"),
        "Period": params.get("Period", 1),
        "TradePrice": price.get("TradePrice"),
        "DetailInfos": {
            "DetailInfo": [
                {"Resource": item.get("Resource"), "TradePrice": item.get("TradePrice")}
                for item in (details if isinstance(details, list) else [])
                if isinstance(item, dict)
            ]
        },
    }
