"""Synthetic BSS business responses for real-model coupon acceptance tests only."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CouponCase:
    title: str
    cash: int | None
    requires_items: bool = False


CASES = {
    "applicable": CouponCase("300 元可用余额覆盖 240 元计算费", 0, True),
    "balance_60": CouponCase("面值 300、余额 60，需补现金 180", 180, True),
    "prepaid_only": CouponCase("仅限包年包月新购，不适用按量账单", 240),
    "gpu_excluded": CouponCase("支持 ECS，但明确排除 GPU", 240),
    "scope_unknown": CouponCase("只确认 ECS 商品，GPU 条件不足", None, True),
    "expired": CouponCase("有余额但已经过期", 240),
    "threshold": CouponCase("满 1000 减 300，本次 240 不达门槛", 240),
    "ecs_blacklisted": CouponCase("ECS 在不适用商品黑名单", 240, True),
    "query_denied": CouponCase("查询权限不足，不能解释为没有券", None),
}


def coupon(case: str) -> dict[str, Any]:
    index = list(CASES).index(case)
    value: dict[str, Any] = {
        "CouponId": 9900000000000 + index,
        "CouponNo": f"STUDENT-300-{index:02d}",
        "CouponType": "VOUCHER", "CouponTypeName": "抵用券",
        "Amount": "300.00", "RemainAmount": "300.00", "Currency": "CNY",
        "Status": "AVAILABLE", "StartTime": "2026-01-01T00:00Z", "EndTime": "2099-01-01T00:00Z",
        "YhOrderTypes": ["阿里云按量付费账单"], "MoneyLimit": "无订单金额限制",
        "OrderTimeRule": "按量付费账单无最低使用时长限制", "FirstBuy": False,
        "ItemNames": ["云服务器ECS"], "SuitItemType": "white",
        "Remark": (
            "学生优惠券。适用于 cn-beijing 地域的 ecs.gn6i-c16g1.4xlarge GPU 实例按量计算费用，"
            "无首购或活动渠道限制。系统盘、EIP 和公网流量费用不在抵扣范围内。"
        ),
    }
    if case == "balance_60":
        value["RemainAmount"] = "60.00"
    elif case == "prepaid_only":
        value.update(YhOrderTypes=["新购"], OrderTimeRule="仅包年包月新购订单", Remark="仅限包年包月新购。")
    elif case == "gpu_excluded":
        value["Remark"] = "仅支持非 GPU ECS 按量计算费用；明确排除所有 GPU 实例。"
    elif case == "scope_unknown":
        value["Remark"] = "学生券；实例规格、GPU、地域及活动渠道限制需另行核实。"
    elif case == "expired":
        value.update(Status="EXPIRED", StartTime="2019-01-01T00:00Z", EndTime="2020-01-01T00:00Z")
    elif case == "threshold":
        value.update(CouponType="CERTAIN", CouponTypeName="满减券", MoneyLimit="满1000元减300元")
    elif case == "ecs_blacklisted":
        value.update(SuitItemType="black", Remark="不适用商品以商品黑名单为准；黑名单优先。")
    return value


def response(case: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Honor the documented IDs and pagination instead of returning a canned page."""
    value = coupon(case)
    if action == "DescribeCoupon":
        rows = [value]
        if params.get("CouponId") is not None and str(params["CouponId"]) != str(value["CouponId"]):
            rows = []
    elif action == "DescribeCouponItemList":
        rows = [{"Code": "vm", "Name": "云服务器ECS"}]
        if str(params.get("CouponId", "")) != str(value["CouponId"]):
            rows = []
    else:
        raise ValueError("Unsupported coupon fixture action")
    page = int(params.get("CurrentPage", 1))
    size = int(params.get("PageSize", 20))
    if page < 1 or not 1 <= size <= 100:
        raise ValueError("Invalid coupon fixture pagination")
    return {
        # Do not expose the scenario name or expected decision in model-visible IDs.
        "RequestId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"coupon-test/{case}/{action}/{page}")).upper(),
        "CurrentPage": page, "PageSize": size, "TotalCount": len(rows),
        "Data": copy.deepcopy(rows[(page - 1) * size:page * size]),
    }
