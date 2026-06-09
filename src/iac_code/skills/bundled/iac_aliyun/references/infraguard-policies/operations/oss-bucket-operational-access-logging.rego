package infraguard.rules.aliyun.oss_bucket_operational_access_logging

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "oss-bucket-operational-access-logging",
    "severity": "medium",
    "name": {
        "en": "OSS bucket must enable logging",
        "zh": "OSS Bucket 必须启用日志",
        "ja": "ALIYUN::OSS::Bucket には LoggingConfiguration を設定する必要があります",
        "de": "Für ALIYUN::OSS::Bucket muss LoggingConfiguration konfiguriert sein",
        "es": "ALIYUN::OSS::Bucket debe tener LoggingConfiguration configurado",
        "fr": "ALIYUN::OSS::Bucket doit avoir LoggingConfiguration configuré",
        "pt": "ALIYUN::OSS::Bucket deve ter LoggingConfiguration configurado"
    },
    "description": {
        "en": "Checks OSS bucket must enable logging",
        "zh": "检查OSS Bucket 必须启用日志",
        "ja": "ALIYUN::OSS::Bucket に LoggingConfiguration が設定されていることを確認します",
        "de": "Prüft, ob LoggingConfiguration für ALIYUN::OSS::Bucket konfiguriert ist",
        "es": "Comprueba que ALIYUN::OSS::Bucket tenga LoggingConfiguration configurado",
        "fr": "Vérifie que ALIYUN::OSS::Bucket a LoggingConfiguration configuré",
        "pt": "Verifica se ALIYUN::OSS::Bucket tem LoggingConfiguration configurado"
    },
    "reason": {
        "en": "OSS bucket must enable logging is not satisfied.",
        "zh": "OSS Bucket 必须启用日志未满足。",
        "ja": "ALIYUN::OSS::Bucket に LoggingConfiguration が設定されていません。",
        "de": "Für ALIYUN::OSS::Bucket ist LoggingConfiguration nicht konfiguriert.",
        "es": "ALIYUN::OSS::Bucket no tiene LoggingConfiguration configurado.",
        "fr": "ALIYUN::OSS::Bucket n'a pas LoggingConfiguration configuré.",
        "pt": "ALIYUN::OSS::Bucket não tem LoggingConfiguration configurado."
    },
    "recommendation": {
        "en": "Configure LoggingConfiguration on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::OSS::Bucket に LoggingConfiguration を設定してください。",
        "de": "Konfigurieren Sie LoggingConfiguration für ALIYUN::OSS::Bucket, um die Richtlinie zu erfüllen.",
        "es": "Configure LoggingConfiguration en ALIYUN::OSS::Bucket para cumplir la política.",
        "fr": "Configurez LoggingConfiguration sur ALIYUN::OSS::Bucket pour satisfaire la politique.",
        "pt": "Configure LoggingConfiguration em ALIYUN::OSS::Bucket para atender à política."
    },
    "resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LoggingConfiguration"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LoggingConfiguration")
}
