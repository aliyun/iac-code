package infraguard.rules.aliyun.oss_storage_class_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "oss-storage-class-required",
    "severity": "medium",
    "name": {
        "en": "OSS bucket must set storage class",
        "zh": "OSS Bucket 必须设置存储类型",
        "ja": "ALIYUN::OSS::Bucket には StorageClass を設定する必要があります",
        "de": "Für ALIYUN::OSS::Bucket muss StorageClass konfiguriert sein",
        "es": "ALIYUN::OSS::Bucket debe tener StorageClass configurado",
        "fr": "ALIYUN::OSS::Bucket doit avoir StorageClass configuré",
        "pt": "ALIYUN::OSS::Bucket deve ter StorageClass configurado"
    },
    "description": {
        "en": "Checks OSS bucket must set storage class",
        "zh": "检查OSS Bucket 必须设置存储类型",
        "ja": "ALIYUN::OSS::Bucket に StorageClass が設定されていることを確認します",
        "de": "Prüft, ob StorageClass für ALIYUN::OSS::Bucket konfiguriert ist",
        "es": "Comprueba que ALIYUN::OSS::Bucket tenga StorageClass configurado",
        "fr": "Vérifie que ALIYUN::OSS::Bucket a StorageClass configuré",
        "pt": "Verifica se ALIYUN::OSS::Bucket tem StorageClass configurado"
    },
    "reason": {
        "en": "OSS bucket must set storage class is not satisfied.",
        "zh": "OSS Bucket 必须设置存储类型未满足。",
        "ja": "ALIYUN::OSS::Bucket に StorageClass が設定されていません。",
        "de": "Für ALIYUN::OSS::Bucket ist StorageClass nicht konfiguriert.",
        "es": "ALIYUN::OSS::Bucket no tiene StorageClass configurado.",
        "fr": "ALIYUN::OSS::Bucket n'a pas StorageClass configuré.",
        "pt": "ALIYUN::OSS::Bucket não tem StorageClass configurado."
    },
    "recommendation": {
        "en": "Configure StorageClass on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 StorageClass 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::OSS::Bucket に StorageClass を設定してください。",
        "de": "Konfigurieren Sie StorageClass für ALIYUN::OSS::Bucket, um die Richtlinie zu erfüllen.",
        "es": "Configure StorageClass en ALIYUN::OSS::Bucket para cumplir la política.",
        "fr": "Configurez StorageClass sur ALIYUN::OSS::Bucket pour satisfaire la politique.",
        "pt": "Configure StorageClass em ALIYUN::OSS::Bucket para atender à política."
    },
    "resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "StorageClass"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "StorageClass")
}
