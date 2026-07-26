from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _append_capture(payload: dict[str, Any]) -> None:
    path_value = os.environ.get("IAC_CODE_E2E_ALIYUN_CAPTURE", "")
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


if os.environ.get("IAC_CODE_E2E_ALIYUN_TRANSPORT_FIXTURE") == "1":
    from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse, PreparedTransportCall

    async def _fixture_execute(self: PreparedTransportCall, *, budget: Any) -> NormalizedApiResponse:
        del budget
        body_by_action = {
            "DescribeVpcs": {
                "RequestId": "request-e2e-describe-vpcs",
                "PageNumber": 1,
                "PageSize": 10,
                "TotalCount": 1,
                "Vpcs": {
                    "Vpc": [
                        {
                            "VpcId": "vpc-e2e-fixture",
                            "VpcName": "contract-e2e",
                            "CidrBlock": "172.16.0.0/12",
                            "Status": "Available",
                        }
                    ]
                },
            },
            "DescribeVSwitches": {
                "RequestId": "request-e2e-describe-vswitches",
                "VSwitches": {"VSwitch": []},
            },
        }
        body = body_by_action.get(
            self.contract.action,
            {"RequestId": f"request-e2e-{self.contract.action.casefold()}", "Success": True},
        )
        _append_capture(
            {
                "product": self.contract.product,
                "version": self.contract.version,
                "action": self.contract.action,
                "method": self.request.method,
                "endpoint": self.endpoint.endpoint,
                "response_body": body,
            }
        )
        sentinel = os.environ.get("IAC_CODE_E2E_ALIYUN_HEADER_SENTINEL", "e2e-internal-header-value")
        payload = json.dumps(body, ensure_ascii=False).encode()
        return NormalizedApiResponse(
            status=200,
            headers={"content-type": "application/json", "x-e2e-internal": sentinel},
            body=body,
            content_type="application/json",
            content_encoding=None,
            size=len(payload),
        )

    PreparedTransportCall.execute = _fixture_execute

    from iac_code.tools.cloud.aliyun.ros_stack import RosStack
    from iac_code.tools.cloud.types import ResourceStatus, StackStatus

    async def _fixture_stack_call_action(
        self: RosStack,
        action: str,
        params: dict[str, Any],
        region: str,
        **kwargs: Any,
    ) -> str:
        del self, params, region, kwargs
        if action != "CreateStack":
            raise ValueError(f"E2E ROS fixture does not support {action}")
        return "stack-e2e-fixture"

    async def _fixture_stack_status(self: RosStack, stack_id: str, region: str) -> StackStatus:
        del self, region
        return StackStatus(
            stack_id=stack_id,
            stack_name="contract-e2e-vswitch",
            status="CREATE_COMPLETE",
            status_reason="",
            progress_percentage=100.0,
        )

    async def _fixture_stack_resources(self: RosStack, stack_id: str, region: str) -> list[ResourceStatus]:
        del self, stack_id, region
        return [
            ResourceStatus(
                name="VSwitch",
                resource_type="ALIYUN::ECS::VSwitch",
                status="CREATE_COMPLETE",
                status_reason="",
            )
        ]

    RosStack.call_action = _fixture_stack_call_action
    RosStack.get_stack_status = _fixture_stack_status
    RosStack.get_stack_resources = _fixture_stack_resources
    RosStack.poll_interval = 0
