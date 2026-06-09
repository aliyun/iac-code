package infraguard.rules.aliyun.rds_pay_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-pay-type-required",
    "severity": "medium",
    "name": {
        "en": "RDS instance must set pay type",
        "zh": "RDS 实例必须设置付费类型",
        "ja": "ALIYUN::RDS::DBInstance には PayType を設定する必要があります",
        "de": "Für ALIYUN::RDS::DBInstance muss PayType konfiguriert sein",
        "es": "ALIYUN::RDS::DBInstance debe tener PayType configurado",
        "fr": "ALIYUN::RDS::DBInstance doit avoir PayType configuré",
        "pt": "ALIYUN::RDS::DBInstance deve ter PayType configurado"
    },
    "description": {
        "en": "Checks RDS instance must set pay type",
        "zh": "检查RDS 实例必须设置付费类型",
        "ja": "ALIYUN::RDS::DBInstance に PayType が設定されていることを確認します",
        "de": "Prüft, ob PayType für ALIYUN::RDS::DBInstance konfiguriert ist",
        "es": "Comprueba que ALIYUN::RDS::DBInstance tenga PayType configurado",
        "fr": "Vérifie que ALIYUN::RDS::DBInstance a PayType configuré",
        "pt": "Verifica se ALIYUN::RDS::DBInstance tem PayType configurado"
    },
    "reason": {
        "en": "RDS instance must set pay type is not satisfied.",
        "zh": "RDS 实例必须设置付费类型未满足。",
        "ja": "ALIYUN::RDS::DBInstance に PayType が設定されていません。",
        "de": "Für ALIYUN::RDS::DBInstance ist PayType nicht konfiguriert.",
        "es": "ALIYUN::RDS::DBInstance no tiene PayType configurado.",
        "fr": "ALIYUN::RDS::DBInstance n'a pas PayType configuré.",
        "pt": "ALIYUN::RDS::DBInstance não tem PayType configurado."
    },
    "recommendation": {
        "en": "Configure PayType on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 PayType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::RDS::DBInstance に PayType を設定してください。",
        "de": "Konfigurieren Sie PayType für ALIYUN::RDS::DBInstance, um die Richtlinie zu erfüllen.",
        "es": "Configure PayType en ALIYUN::RDS::DBInstance para cumplir la política.",
        "fr": "Configurez PayType sur ALIYUN::RDS::DBInstance pour satisfaire la politique.",
        "pt": "Configure PayType em ALIYUN::RDS::DBInstance para atender à política."
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "PayType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "PayType")
}
