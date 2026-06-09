package infraguard.rules.aliyun.ess_group_health_check

import rego.v1

import data.infraguard.helpers

rule_meta := {
    "id": "ess-group-health-check",
    "severity": "medium",
    "name": {"en": "ESS Group Health Check", "zh": "ESS 伸缩组健康检查", "ja": "ALIYUN::ESS::ScalingGroup には HealthCheckType を設定する必要があります", "de": "Für ALIYUN::ESS::ScalingGroup muss HealthCheckType konfiguriert sein", "es": "ALIYUN::ESS::ScalingGroup debe tener HealthCheckType configurado", "fr": "ALIYUN::ESS::ScalingGroup doit avoir HealthCheckType configuré", "pt": "ALIYUN::ESS::ScalingGroup deve ter HealthCheckType configurado"},
    "description": {"en": "ESS scaling groups should configure health checks before automatic scaling is used.", "zh": "ESS 伸缩组应配置健康检查。", "ja": "ALIYUN::ESS::ScalingGroup に HealthCheckType が設定されていることを確認します", "de": "Prüft, ob HealthCheckType für ALIYUN::ESS::ScalingGroup konfiguriert ist", "es": "Comprueba que ALIYUN::ESS::ScalingGroup tenga HealthCheckType configurado", "fr": "Vérifie que ALIYUN::ESS::ScalingGroup a HealthCheckType configuré", "pt": "Verifica se ALIYUN::ESS::ScalingGroup tem HealthCheckType configurado"},
    "reason": {"en": "The scaling group does not configure HealthCheckType.", "zh": "伸缩组未配置 HealthCheckType。", "ja": "ALIYUN::ESS::ScalingGroup に HealthCheckType が設定されていません。", "de": "Für ALIYUN::ESS::ScalingGroup ist HealthCheckType nicht konfiguriert.", "es": "ALIYUN::ESS::ScalingGroup no tiene HealthCheckType configurado.", "fr": "ALIYUN::ESS::ScalingGroup n'a pas HealthCheckType configuré.", "pt": "ALIYUN::ESS::ScalingGroup não tem HealthCheckType configurado."},
    "recommendation": {"en": "Configure HealthCheckType for the ESS scaling group.", "zh": "为 ESS 伸缩组配置 HealthCheckType。", "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingGroup に HealthCheckType を設定してください。", "de": "Konfigurieren Sie HealthCheckType für ALIYUN::ESS::ScalingGroup, um die Richtlinie zu erfüllen.", "es": "Configure HealthCheckType en ALIYUN::ESS::ScalingGroup para cumplir la política.", "fr": "Configurez HealthCheckType sur ALIYUN::ESS::ScalingGroup pour satisfaire la politique.", "pt": "Configure HealthCheckType em ALIYUN::ESS::ScalingGroup para atender à política."},
    "resource_types": ["ALIYUN::ESS::ScalingGroup"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not helpers.has_property(resource, "HealthCheckType")
    result := {"id": rule_meta.id, "resource_id": name, "violation_path": ["Properties", "HealthCheckType"], "meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation}}
}
