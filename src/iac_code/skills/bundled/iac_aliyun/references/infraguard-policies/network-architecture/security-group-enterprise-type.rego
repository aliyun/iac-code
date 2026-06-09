package infraguard.rules.aliyun.security_group_enterprise_type

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-group-enterprise-type",
    "severity": "medium",
    "name": {
        "en": "Security group must set type",
        "zh": "安全组必须设置类型",
        "ja": "ALIYUN::ECS::SecurityGroup には SecurityGroupType を設定する必要があります",
        "de": "Für ALIYUN::ECS::SecurityGroup muss SecurityGroupType konfiguriert sein",
        "es": "ALIYUN::ECS::SecurityGroup debe tener SecurityGroupType configurado",
        "fr": "ALIYUN::ECS::SecurityGroup doit avoir SecurityGroupType configuré",
        "pt": "ALIYUN::ECS::SecurityGroup deve ter SecurityGroupType configurado"
    },
    "description": {
        "en": "Checks Security group must set type",
        "zh": "检查安全组必须设置类型",
        "ja": "ALIYUN::ECS::SecurityGroup に SecurityGroupType が設定されていることを確認します",
        "de": "Prüft, ob SecurityGroupType für ALIYUN::ECS::SecurityGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::SecurityGroup tenga SecurityGroupType configurado",
        "fr": "Vérifie que ALIYUN::ECS::SecurityGroup a SecurityGroupType configuré",
        "pt": "Verifica se ALIYUN::ECS::SecurityGroup tem SecurityGroupType configurado"
    },
    "reason": {
        "en": "Security group must set type is not satisfied.",
        "zh": "安全组必须设置类型未满足。",
        "ja": "ALIYUN::ECS::SecurityGroup に SecurityGroupType が設定されていません。",
        "de": "Für ALIYUN::ECS::SecurityGroup ist SecurityGroupType nicht konfiguriert.",
        "es": "ALIYUN::ECS::SecurityGroup no tiene SecurityGroupType configurado.",
        "fr": "ALIYUN::ECS::SecurityGroup n'a pas SecurityGroupType configuré.",
        "pt": "ALIYUN::ECS::SecurityGroup não tem SecurityGroupType configurado."
    },
    "recommendation": {
        "en": "Configure SecurityGroupType on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::SecurityGroup に SecurityGroupType を設定してください。",
        "de": "Konfigurieren Sie SecurityGroupType für ALIYUN::ECS::SecurityGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure SecurityGroupType en ALIYUN::ECS::SecurityGroup para cumplir la política.",
        "fr": "Configurez SecurityGroupType sur ALIYUN::ECS::SecurityGroup pour satisfaire la politique.",
        "pt": "Configure SecurityGroupType em ALIYUN::ECS::SecurityGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "SecurityGroupType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "SecurityGroupType")
}
