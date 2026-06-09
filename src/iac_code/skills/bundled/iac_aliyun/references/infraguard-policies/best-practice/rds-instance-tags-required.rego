package infraguard.rules.aliyun.rds_instance_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-instance-tags-required",
    "severity": "medium",
    "name": {
        "en": "RDS instance must configure tags",
        "zh": "RDS 实例必须配置标签",
        "ja": "ALIYUN::RDS::DBInstance には Tags を設定する必要があります",
        "de": "Für ALIYUN::RDS::DBInstance muss Tags konfiguriert sein",
        "es": "ALIYUN::RDS::DBInstance debe tener Tags configurado",
        "fr": "ALIYUN::RDS::DBInstance doit avoir Tags configuré",
        "pt": "ALIYUN::RDS::DBInstance deve ter Tags configurado"
    },
    "description": {
        "en": "Checks RDS instance must configure tags",
        "zh": "检查RDS 实例必须配置标签",
        "ja": "ALIYUN::RDS::DBInstance に Tags が設定されていることを確認します",
        "de": "Prüft, ob Tags für ALIYUN::RDS::DBInstance konfiguriert ist",
        "es": "Comprueba que ALIYUN::RDS::DBInstance tenga Tags configurado",
        "fr": "Vérifie que ALIYUN::RDS::DBInstance a Tags configuré",
        "pt": "Verifica se ALIYUN::RDS::DBInstance tem Tags configurado"
    },
    "reason": {
        "en": "RDS instance must configure tags is not satisfied.",
        "zh": "RDS 实例必须配置标签未满足。",
        "ja": "ALIYUN::RDS::DBInstance に Tags が設定されていません。",
        "de": "Für ALIYUN::RDS::DBInstance ist Tags nicht konfiguriert.",
        "es": "ALIYUN::RDS::DBInstance no tiene Tags configurado.",
        "fr": "ALIYUN::RDS::DBInstance n'a pas Tags configuré.",
        "pt": "ALIYUN::RDS::DBInstance não tem Tags configurado."
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 Tags 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::RDS::DBInstance に Tags を設定してください。",
        "de": "Konfigurieren Sie Tags für ALIYUN::RDS::DBInstance, um die Richtlinie zu erfüllen.",
        "es": "Configure Tags en ALIYUN::RDS::DBInstance para cumplir la política.",
        "fr": "Configurez Tags sur ALIYUN::RDS::DBInstance pour satisfaire la politique.",
        "pt": "Configure Tags em ALIYUN::RDS::DBInstance para atender à política."
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Tags"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Tags")
}
