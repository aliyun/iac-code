package infraguard.rules.aliyun.ack_cluster_node_pool_scaling_limits_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ack-cluster-node-pool-scaling-limits-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling group must configure MinSize",
        "zh": "ESS 伸缩组必须配置最小容量",
        "ja": "ALIYUN::ESS::ScalingGroup には MinSize を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingGroup muss MinSize konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingGroup debe tener MinSize configurado",
        "fr": "ALIYUN::ESS::ScalingGroup doit avoir MinSize configuré",
        "pt": "ALIYUN::ESS::ScalingGroup deve ter MinSize configurado"
    },
    "description": {
        "en": "Checks ESS scaling group must configure MinSize",
        "zh": "检查ESS 伸缩组必须配置最小容量",
        "ja": "ALIYUN::ESS::ScalingGroup に MinSize が設定されていることを確認します",
        "de": "Prüft, ob MinSize für ALIYUN::ESS::ScalingGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingGroup tenga MinSize configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingGroup a MinSize configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingGroup tem MinSize configurado"
    },
    "reason": {
        "en": "ESS scaling group must configure MinSize is not satisfied.",
        "zh": "ESS 伸缩组必须配置最小容量未满足。",
        "ja": "ALIYUN::ESS::ScalingGroup に MinSize が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingGroup ist MinSize nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingGroup no tiene MinSize configurado.",
        "fr": "ALIYUN::ESS::ScalingGroup n'a pas MinSize configuré.",
        "pt": "ALIYUN::ESS::ScalingGroup não tem MinSize configurado."
    },
    "recommendation": {
        "en": "Configure MinSize on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingGroup に MinSize を設定してください。",
        "de": "Konfigurieren Sie MinSize für ALIYUN::ESS::ScalingGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure MinSize en ALIYUN::ESS::ScalingGroup para cumplir la política.",
        "fr": "Configurez MinSize sur ALIYUN::ESS::ScalingGroup pour satisfaire la politique.",
        "pt": "Configure MinSize em ALIYUN::ESS::ScalingGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "MinSize"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "MinSize")
}
