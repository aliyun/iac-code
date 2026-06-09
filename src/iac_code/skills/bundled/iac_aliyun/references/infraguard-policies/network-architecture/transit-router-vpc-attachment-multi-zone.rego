package infraguard.rules.aliyun.transit_router_vpc_attachment_multi_zone

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "transit-router-vpc-attachment-multi-zone",
    "severity": "high",
    "name": {
        "en": "Transit router VPC attachment must configure zone mapping",
        "zh": "转发路由器 VPC 连接必须配置可用区映射",
        "ja": "转发路由器 VPC 连接必须配置可用区映射",
        "de": "转发路由器 VPC 连接必须配置可用区映射",
        "es": "转发路由器 VPC 连接必须配置可用区映射",
        "fr": "转发路由器 VPC 连接必须配置可用区映射",
        "pt": "转发路由器 VPC 连接必须配置可用区映射"
    },
    "description": {
        "en": "Checks Transit router VPC attachment must configure zone mapping",
        "zh": "检查转发路由器 VPC 连接必须配置可用区映射",
        "ja": "检查转发路由器 VPC 连接必须配置可用区映射",
        "de": "检查转发路由器 VPC 连接必须配置可用区映射",
        "es": "检查转发路由器 VPC 连接必须配置可用区映射",
        "fr": "检查转发路由器 VPC 连接必须配置可用区映射",
        "pt": "检查转发路由器 VPC 连接必须配置可用区映射"
    },
    "reason": {
        "en": "Transit router VPC attachment must configure zone mapping is not satisfied.",
        "zh": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "ja": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "de": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "es": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "fr": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "pt": "转发路由器 VPC 连接必须配置可用区映射未满足。"
    },
    "recommendation": {
        "en": "Configure ZoneMappings on ALIYUN::CEN::TransitRouterVpcAttachment to satisfy the policy.",
        "zh": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "ja": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "de": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "es": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "fr": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "pt": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。"
    },
    "resource_types": ["ALIYUN::CEN::TransitRouterVpcAttachment"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CEN::TransitRouterVpcAttachment")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ZoneMappings"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ZoneMappings")
}
