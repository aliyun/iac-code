package infraguard.rules.aliyun.ess_scaling_group_attach_multi_switch

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-group-attach-multi-switch",
    "severity": "high",
    "name": {
        "en": "ESS scaling group must configure VSwitches for elasticity",
        "zh": "ESS 伸缩组必须配置交换机以支持弹性",
        "ja": "ALIYUN::ESS::ScalingGroup には VSwitchIds を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingGroup muss VSwitchIds konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingGroup debe tener VSwitchIds configurado",
        "fr": "ALIYUN::ESS::ScalingGroup doit avoir VSwitchIds configuré",
        "pt": "ALIYUN::ESS::ScalingGroup deve ter VSwitchIds configurado"
    },
    "description": {
        "en": "Checks ESS scaling group must configure VSwitches for elasticity",
        "zh": "检查ESS 伸缩组必须配置交换机以支持弹性",
        "ja": "ALIYUN::ESS::ScalingGroup に VSwitchIds が設定されていることを確認します",
        "de": "Prüft, ob VSwitchIds für ALIYUN::ESS::ScalingGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingGroup tenga VSwitchIds configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingGroup a VSwitchIds configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingGroup tem VSwitchIds configurado"
    },
    "reason": {
        "en": "ESS scaling group must configure VSwitches for elasticity is not satisfied.",
        "zh": "ESS 伸缩组必须配置交换机以支持弹性未满足。",
        "ja": "ALIYUN::ESS::ScalingGroup に VSwitchIds が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingGroup ist VSwitchIds nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingGroup no tiene VSwitchIds configurado.",
        "fr": "ALIYUN::ESS::ScalingGroup n'a pas VSwitchIds configuré.",
        "pt": "ALIYUN::ESS::ScalingGroup não tem VSwitchIds configurado."
    },
    "recommendation": {
        "en": "Configure VSwitchIds on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 VSwitchIds 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingGroup に VSwitchIds を設定してください。",
        "de": "Konfigurieren Sie VSwitchIds für ALIYUN::ESS::ScalingGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure VSwitchIds en ALIYUN::ESS::ScalingGroup para cumplir la política.",
        "fr": "Configurez VSwitchIds sur ALIYUN::ESS::ScalingGroup pour satisfaire la politique.",
        "pt": "Configure VSwitchIds em ALIYUN::ESS::ScalingGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VSwitchIds"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VSwitchIds")
}
