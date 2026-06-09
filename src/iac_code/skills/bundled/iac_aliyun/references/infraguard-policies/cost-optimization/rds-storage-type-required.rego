package infraguard.rules.aliyun.rds_storage_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-storage-type-required",
    "severity": "medium",
    "name": {
        "en": "RDS instance must set storage type",
        "zh": "RDS 实例必须设置存储类型",
        "ja": "ALIYUN::RDS::DBInstance には DBInstanceStorageType を設定する必要があります",
        "de": "Für ALIYUN::RDS::DBInstance muss DBInstanceStorageType konfiguriert sein",
        "es": "ALIYUN::RDS::DBInstance debe tener DBInstanceStorageType configurado",
        "fr": "ALIYUN::RDS::DBInstance doit avoir DBInstanceStorageType configuré",
        "pt": "ALIYUN::RDS::DBInstance deve ter DBInstanceStorageType configurado"
    },
    "description": {
        "en": "Checks RDS instance must set storage type",
        "zh": "检查RDS 实例必须设置存储类型",
        "ja": "ALIYUN::RDS::DBInstance に DBInstanceStorageType が設定されていることを確認します",
        "de": "Prüft, ob DBInstanceStorageType für ALIYUN::RDS::DBInstance konfiguriert ist",
        "es": "Comprueba que ALIYUN::RDS::DBInstance tenga DBInstanceStorageType configurado",
        "fr": "Vérifie que ALIYUN::RDS::DBInstance a DBInstanceStorageType configuré",
        "pt": "Verifica se ALIYUN::RDS::DBInstance tem DBInstanceStorageType configurado"
    },
    "reason": {
        "en": "RDS instance must set storage type is not satisfied.",
        "zh": "RDS 实例必须设置存储类型未满足。",
        "ja": "ALIYUN::RDS::DBInstance に DBInstanceStorageType が設定されていません。",
        "de": "Für ALIYUN::RDS::DBInstance ist DBInstanceStorageType nicht konfiguriert.",
        "es": "ALIYUN::RDS::DBInstance no tiene DBInstanceStorageType configurado.",
        "fr": "ALIYUN::RDS::DBInstance n'a pas DBInstanceStorageType configuré.",
        "pt": "ALIYUN::RDS::DBInstance não tem DBInstanceStorageType configurado."
    },
    "recommendation": {
        "en": "Configure DBInstanceStorageType on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::RDS::DBInstance に DBInstanceStorageType を設定してください。",
        "de": "Konfigurieren Sie DBInstanceStorageType für ALIYUN::RDS::DBInstance, um die Richtlinie zu erfüllen.",
        "es": "Configure DBInstanceStorageType en ALIYUN::RDS::DBInstance para cumplir la política.",
        "fr": "Configurez DBInstanceStorageType sur ALIYUN::RDS::DBInstance pour satisfaire la politique.",
        "pt": "Configure DBInstanceStorageType em ALIYUN::RDS::DBInstance para atender à política."
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DBInstanceStorageType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DBInstanceStorageType")
}
