package infraguard.rules.aliyun.cen_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "cen-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "CEN instance must configure name",
        "zh": "CEN 实例必须配置名称",
        "ja": "ALIYUN::CEN::CenInstance には Name を設定する必要があります",
        "de": "Für ALIYUN::CEN::CenInstance muss Name konfiguriert sein",
        "es": "ALIYUN::CEN::CenInstance debe tener Name configurado",
        "fr": "ALIYUN::CEN::CenInstance doit avoir Name configuré",
        "pt": "ALIYUN::CEN::CenInstance deve ter Name configurado"
    },
    "description": {
        "en": "Checks CEN instance must configure name",
        "zh": "检查CEN 实例必须配置名称",
        "ja": "ALIYUN::CEN::CenInstance に Name が設定されていることを確認します",
        "de": "Prüft, ob Name für ALIYUN::CEN::CenInstance konfiguriert ist",
        "es": "Comprueba que ALIYUN::CEN::CenInstance tenga Name configurado",
        "fr": "Vérifie que ALIYUN::CEN::CenInstance a Name configuré",
        "pt": "Verifica se ALIYUN::CEN::CenInstance tem Name configurado"
    },
    "reason": {
        "en": "CEN instance must configure name is not satisfied.",
        "zh": "CEN 实例必须配置名称未满足。",
        "ja": "ALIYUN::CEN::CenInstance に Name が設定されていません。",
        "de": "Für ALIYUN::CEN::CenInstance ist Name nicht konfiguriert.",
        "es": "ALIYUN::CEN::CenInstance no tiene Name configurado.",
        "fr": "ALIYUN::CEN::CenInstance n'a pas Name configuré.",
        "pt": "ALIYUN::CEN::CenInstance não tem Name configurado."
    },
    "recommendation": {
        "en": "Configure Name on ALIYUN::CEN::CenInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::CEN::CenInstance に Name を設定してください。",
        "de": "Konfigurieren Sie Name für ALIYUN::CEN::CenInstance, um die Richtlinie zu erfüllen.",
        "es": "Configure Name en ALIYUN::CEN::CenInstance para cumplir la política.",
        "fr": "Configurez Name sur ALIYUN::CEN::CenInstance pour satisfaire la politique.",
        "pt": "Configure Name em ALIYUN::CEN::CenInstance para atender à política."
    },
    "resource_types": ["ALIYUN::CEN::CenInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CEN::CenInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Name"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Name")
}
