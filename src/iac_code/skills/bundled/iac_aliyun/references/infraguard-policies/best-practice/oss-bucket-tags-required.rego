package infraguard.rules.aliyun.oss_bucket_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "oss-bucket-tags-required",
    "severity": "medium",
    "name": {
        "en": "OSS bucket must configure tags",
        "zh": "OSS Bucket 必须配置标签",
        "ja": "ALIYUN::OSS::Bucket には Tags を設定する必要があります",
        "de": "Für ALIYUN::OSS::Bucket muss Tags konfiguriert sein",
        "es": "ALIYUN::OSS::Bucket debe tener Tags configurado",
        "fr": "ALIYUN::OSS::Bucket doit avoir Tags configuré",
        "pt": "ALIYUN::OSS::Bucket deve ter Tags configurado"
    },
    "description": {
        "en": "Checks OSS bucket must configure tags",
        "zh": "检查OSS Bucket 必须配置标签",
        "ja": "ALIYUN::OSS::Bucket に Tags が設定されていることを確認します",
        "de": "Prüft, ob Tags für ALIYUN::OSS::Bucket konfiguriert ist",
        "es": "Comprueba que ALIYUN::OSS::Bucket tenga Tags configurado",
        "fr": "Vérifie que ALIYUN::OSS::Bucket a Tags configuré",
        "pt": "Verifica se ALIYUN::OSS::Bucket tem Tags configurado"
    },
    "reason": {
        "en": "OSS bucket must configure tags is not satisfied.",
        "zh": "OSS Bucket 必须配置标签未满足。",
        "ja": "ALIYUN::OSS::Bucket に Tags が設定されていません。",
        "de": "Für ALIYUN::OSS::Bucket ist Tags nicht konfiguriert.",
        "es": "ALIYUN::OSS::Bucket no tiene Tags configurado.",
        "fr": "ALIYUN::OSS::Bucket n'a pas Tags configuré.",
        "pt": "ALIYUN::OSS::Bucket não tem Tags configurado."
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 Tags 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::OSS::Bucket に Tags を設定してください。",
        "de": "Konfigurieren Sie Tags für ALIYUN::OSS::Bucket, um die Richtlinie zu erfüllen.",
        "es": "Configure Tags en ALIYUN::OSS::Bucket para cumplir la política.",
        "fr": "Configurez Tags sur ALIYUN::OSS::Bucket pour satisfaire la politique.",
        "pt": "Configure Tags em ALIYUN::OSS::Bucket para atender à política."
    },
    "resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
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
