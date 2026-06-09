package infraguard.rules.aliyun.ess_scaling_group_cooldown_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-group-cooldown-configured",
    "severity": "medium",
    "name": {
        "en": "ESS scaling group must configure cooldown",
        "zh": "ESS 伸缩组必须配置冷却时间",
        "ja": "ALIYUN::ESS::ScalingGroup には DefaultCooldown を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingGroup muss DefaultCooldown konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingGroup debe tener DefaultCooldown configurado",
        "fr": "ALIYUN::ESS::ScalingGroup doit avoir DefaultCooldown configuré",
        "pt": "ALIYUN::ESS::ScalingGroup deve ter DefaultCooldown configurado"
    },
    "description": {
        "en": "Checks ESS scaling group must configure cooldown",
        "zh": "检查ESS 伸缩组必须配置冷却时间",
        "ja": "ALIYUN::ESS::ScalingGroup に DefaultCooldown が設定されていることを確認します",
        "de": "Prüft, ob DefaultCooldown für ALIYUN::ESS::ScalingGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingGroup tenga DefaultCooldown configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingGroup a DefaultCooldown configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingGroup tem DefaultCooldown configurado"
    },
    "reason": {
        "en": "ESS scaling group must configure cooldown is not satisfied.",
        "zh": "ESS 伸缩组必须配置冷却时间未满足。",
        "ja": "ALIYUN::ESS::ScalingGroup に DefaultCooldown が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingGroup ist DefaultCooldown nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingGroup no tiene DefaultCooldown configurado.",
        "fr": "ALIYUN::ESS::ScalingGroup n'a pas DefaultCooldown configuré.",
        "pt": "ALIYUN::ESS::ScalingGroup não tem DefaultCooldown configurado."
    },
    "recommendation": {
        "en": "Configure DefaultCooldown on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 DefaultCooldown 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingGroup に DefaultCooldown を設定してください。",
        "de": "Konfigurieren Sie DefaultCooldown für ALIYUN::ESS::ScalingGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure DefaultCooldown en ALIYUN::ESS::ScalingGroup para cumplir la política.",
        "fr": "Configurez DefaultCooldown sur ALIYUN::ESS::ScalingGroup pour satisfaire la politique.",
        "pt": "Configure DefaultCooldown em ALIYUN::ESS::ScalingGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DefaultCooldown"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DefaultCooldown")
}
