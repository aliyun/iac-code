package infraguard.rules.aliyun.eip_explicit_bandwidth_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "eip-explicit-bandwidth-required",
    "severity": "medium",
    "name": {
        "en": "EIP must configure bandwidth",
        "zh": "EIP 必须配置带宽",
        "ja": "ALIYUN::VPC::EIP には Bandwidth を設定する必要があります",
        "de": "Für ALIYUN::VPC::EIP muss Bandwidth konfiguriert sein",
        "es": "ALIYUN::VPC::EIP debe tener Bandwidth configurado",
        "fr": "ALIYUN::VPC::EIP doit avoir Bandwidth configuré",
        "pt": "ALIYUN::VPC::EIP deve ter Bandwidth configurado"
    },
    "description": {
        "en": "Checks EIP must configure bandwidth",
        "zh": "检查EIP 必须配置带宽",
        "ja": "ALIYUN::VPC::EIP に Bandwidth が設定されていることを確認します",
        "de": "Prüft, ob Bandwidth für ALIYUN::VPC::EIP konfiguriert ist",
        "es": "Comprueba que ALIYUN::VPC::EIP tenga Bandwidth configurado",
        "fr": "Vérifie que ALIYUN::VPC::EIP a Bandwidth configuré",
        "pt": "Verifica se ALIYUN::VPC::EIP tem Bandwidth configurado"
    },
    "reason": {
        "en": "EIP must configure bandwidth is not satisfied.",
        "zh": "EIP 必须配置带宽未满足。",
        "ja": "ALIYUN::VPC::EIP に Bandwidth が設定されていません。",
        "de": "Für ALIYUN::VPC::EIP ist Bandwidth nicht konfiguriert.",
        "es": "ALIYUN::VPC::EIP no tiene Bandwidth configurado.",
        "fr": "ALIYUN::VPC::EIP n'a pas Bandwidth configuré.",
        "pt": "ALIYUN::VPC::EIP não tem Bandwidth configurado."
    },
    "recommendation": {
        "en": "Configure Bandwidth on ALIYUN::VPC::EIP to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::VPC::EIP に Bandwidth を設定してください。",
        "de": "Konfigurieren Sie Bandwidth für ALIYUN::VPC::EIP, um die Richtlinie zu erfüllen.",
        "es": "Configure Bandwidth en ALIYUN::VPC::EIP para cumplir la política.",
        "fr": "Configurez Bandwidth sur ALIYUN::VPC::EIP pour satisfaire la politique.",
        "pt": "Configure Bandwidth em ALIYUN::VPC::EIP para atender à política."
    },
    "resource_types": ["ALIYUN::VPC::EIP"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::VPC::EIP")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Bandwidth"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Bandwidth")
}
