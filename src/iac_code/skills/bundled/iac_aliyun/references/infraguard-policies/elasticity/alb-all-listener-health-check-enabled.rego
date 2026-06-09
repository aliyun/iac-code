package infraguard.rules.aliyun.alb_all_listener_health_check_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
    "id": "alb-all-listener-health-check-enabled",
    "severity": "medium",
    "name": {"en": "ALB Listener Health Check Enabled", "zh": "ALB 监听开启健康检查", "ja": "ALIYUN::ALB::Listener には HealthCheckConfig を設定する必要があります", "de": "Für ALIYUN::ALB::Listener muss HealthCheckConfig konfiguriert sein", "es": "ALIYUN::ALB::Listener debe tener HealthCheckConfig configurado", "fr": "ALIYUN::ALB::Listener doit avoir HealthCheckConfig configuré", "pt": "ALIYUN::ALB::Listener deve ter HealthCheckConfig configurado"},
    "description": {"en": "ALB listeners should configure health checks before serving elastic traffic.", "zh": "ALB 监听应配置健康检查。", "ja": "ALIYUN::ALB::Listener に HealthCheckConfig が設定されていることを確認します", "de": "Prüft, ob HealthCheckConfig für ALIYUN::ALB::Listener konfiguriert ist", "es": "Comprueba que ALIYUN::ALB::Listener tenga HealthCheckConfig configurado", "fr": "Vérifie que ALIYUN::ALB::Listener a HealthCheckConfig configuré", "pt": "Verifica se ALIYUN::ALB::Listener tem HealthCheckConfig configurado"},
    "reason": {"en": "The listener does not configure HealthCheckConfig.", "zh": "监听未配置 HealthCheckConfig。", "ja": "ALIYUN::ALB::Listener に HealthCheckConfig が設定されていません。", "de": "Für ALIYUN::ALB::Listener ist HealthCheckConfig nicht konfiguriert.", "es": "ALIYUN::ALB::Listener no tiene HealthCheckConfig configurado.", "fr": "ALIYUN::ALB::Listener n'a pas HealthCheckConfig configuré.", "pt": "ALIYUN::ALB::Listener não tem HealthCheckConfig configurado."},
    "recommendation": {"en": "Configure HealthCheckConfig for ALB listeners.", "zh": "为 ALB 监听配置 HealthCheckConfig。", "ja": "ポリシーを満たすには、ALIYUN::ALB::Listener に HealthCheckConfig を設定してください。", "de": "Konfigurieren Sie HealthCheckConfig für ALIYUN::ALB::Listener, um die Richtlinie zu erfüllen.", "es": "Configure HealthCheckConfig en ALIYUN::ALB::Listener para cumplir la política.", "fr": "Configurez HealthCheckConfig sur ALIYUN::ALB::Listener pour satisfaire la politique.", "pt": "Configure HealthCheckConfig em ALIYUN::ALB::Listener para atender à política."},
    "resource_types": ["ALIYUN::ALB::Listener"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::Listener")
    not helpers.has_property(resource, "HealthCheckConfig")
    result := {"id": rule_meta.id, "resource_id": name, "violation_path": ["Properties", "HealthCheckConfig"], "meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation}}
}
