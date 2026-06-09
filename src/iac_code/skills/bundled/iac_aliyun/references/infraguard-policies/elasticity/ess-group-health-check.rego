package infraguard.rules.aliyun.ess_group_health_check

import rego.v1

import data.infraguard.helpers

rule_meta := {
    "id": "ess-group-health-check",
    "severity": "medium",
    "name": {"en": "ESS Group Health Check", "zh": "ESS 伸缩组健康检查", "ja": "ESS 伸缩组健康检查", "de": "ESS 伸缩组健康检查", "es": "ESS 伸缩组健康检查", "fr": "ESS 伸缩组健康检查", "pt": "ESS 伸缩组健康检查"},
    "description": {"en": "ESS scaling groups should configure health checks before automatic scaling is used.", "zh": "ESS 伸缩组应配置健康检查。", "ja": "ESS 伸缩组应配置健康检查。", "de": "ESS 伸缩组应配置健康检查。", "es": "ESS 伸缩组应配置健康检查。", "fr": "ESS 伸缩组应配置健康检查。", "pt": "ESS 伸缩组应配置健康检查。"},
    "reason": {"en": "The scaling group does not configure HealthCheckType.", "zh": "伸缩组未配置 HealthCheckType。", "ja": "伸缩组未配置 HealthCheckType。", "de": "伸缩组未配置 HealthCheckType。", "es": "伸缩组未配置 HealthCheckType。", "fr": "伸缩组未配置 HealthCheckType。", "pt": "伸缩组未配置 HealthCheckType。"},
    "recommendation": {"en": "Configure HealthCheckType for the ESS scaling group.", "zh": "为 ESS 伸缩组配置 HealthCheckType。", "ja": "为 ESS 伸缩组配置 HealthCheckType。", "de": "为 ESS 伸缩组配置 HealthCheckType。", "es": "为 ESS 伸缩组配置 HealthCheckType。", "fr": "为 ESS 伸缩组配置 HealthCheckType。", "pt": "为 ESS 伸缩组配置 HealthCheckType。"},
    "resource_types": ["ALIYUN::ESS::ScalingGroup"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not helpers.has_property(resource, "HealthCheckType")
    result := {"id": rule_meta.id, "resource_id": name, "violation_path": ["Properties", "HealthCheckType"], "meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation}}
}
