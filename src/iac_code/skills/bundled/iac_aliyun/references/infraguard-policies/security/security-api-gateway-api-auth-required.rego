package infraguard.rules.aliyun.security_api_gateway_api_auth_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-api-gateway-api-auth-required",
    "severity": "high",
    "name": {
        "en": "API Gateway API must configure authentication",
        "zh": "API 网关 API 必须配置认证",
    },
    "description": {
        "en": "Checks API Gateway API must configure authentication",
        "zh": "检查API 网关 API 必须配置认证",
    },
    "reason": {
        "en": "API Gateway API must configure authentication is not satisfied.",
        "zh": "API 网关 API 必须配置认证未满足。",
    },
    "recommendation": {
        "en": "Configure AuthType on ALIYUN::ApiGateway::Api to satisfy the policy.",
        "zh": "请在 ALIYUN::ApiGateway::Api 上配置 AuthType 以满足策略。",
    },
    "resource_types": ["ALIYUN::ApiGateway::Api"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ApiGateway::Api")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AuthType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "AuthType")
}
