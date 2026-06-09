package infraguard.rules.aliyun.alb_all_listener_health_check_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
    "id": "alb-all-listener-health-check-enabled",
    "severity": "medium",
    "name": {"en": "ALB Listener Health Check Enabled", "zh": "ALB 监听开启健康检查", "ja": "ALB 监听开启健康检查", "de": "ALB 监听开启健康检查", "es": "ALB 监听开启健康检查", "fr": "ALB 监听开启健康检查", "pt": "ALB 监听开启健康检查"},
    "description": {"en": "ALB listeners should configure health checks before serving elastic traffic.", "zh": "ALB 监听应配置健康检查。", "ja": "ALB 监听应配置健康检查。", "de": "ALB 监听应配置健康检查。", "es": "ALB 监听应配置健康检查。", "fr": "ALB 监听应配置健康检查。", "pt": "ALB 监听应配置健康检查。"},
    "reason": {"en": "The listener does not configure HealthCheckConfig.", "zh": "监听未配置 HealthCheckConfig。", "ja": "监听未配置 HealthCheckConfig。", "de": "监听未配置 HealthCheckConfig。", "es": "监听未配置 HealthCheckConfig。", "fr": "监听未配置 HealthCheckConfig。", "pt": "监听未配置 HealthCheckConfig。"},
    "recommendation": {"en": "Configure HealthCheckConfig for ALB listeners.", "zh": "为 ALB 监听配置 HealthCheckConfig。", "ja": "为 ALB 监听配置 HealthCheckConfig。", "de": "为 ALB 监听配置 HealthCheckConfig。", "es": "为 ALB 监听配置 HealthCheckConfig。", "fr": "为 ALB 监听配置 HealthCheckConfig。", "pt": "为 ALB 监听配置 HealthCheckConfig。"},
    "resource_types": ["ALIYUN::ALB::Listener"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::Listener")
    not helpers.has_property(resource, "HealthCheckConfig")
    result := {"id": rule_meta.id, "resource_id": name, "violation_path": ["Properties", "HealthCheckConfig"], "meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation}}
}
