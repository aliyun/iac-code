package infraguard.rules.aliyun.mse_cluster_high_availability_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "mse-cluster-high-availability-configured",
    "severity": "medium",
    "name": {
        "en": "MSE cluster must configure replicas",
        "zh": "MSE 集群必须配置副本数",
        "ja": "ALIYUN::MSE::Cluster には Replicas を設定する必要があります",
        "de": "Für ALIYUN::MSE::Cluster muss Replicas konfiguriert sein",
        "es": "ALIYUN::MSE::Cluster debe tener Replicas configurado",
        "fr": "ALIYUN::MSE::Cluster doit avoir Replicas configuré",
        "pt": "ALIYUN::MSE::Cluster deve ter Replicas configurado"
    },
    "description": {
        "en": "Checks MSE cluster must configure replicas",
        "zh": "检查MSE 集群必须配置副本数",
        "ja": "ALIYUN::MSE::Cluster に Replicas が設定されていることを確認します",
        "de": "Prüft, ob Replicas für ALIYUN::MSE::Cluster konfiguriert ist",
        "es": "Comprueba que ALIYUN::MSE::Cluster tenga Replicas configurado",
        "fr": "Vérifie que ALIYUN::MSE::Cluster a Replicas configuré",
        "pt": "Verifica se ALIYUN::MSE::Cluster tem Replicas configurado"
    },
    "reason": {
        "en": "MSE cluster must configure replicas is not satisfied.",
        "zh": "MSE 集群必须配置副本数未满足。",
        "ja": "ALIYUN::MSE::Cluster に Replicas が設定されていません。",
        "de": "Für ALIYUN::MSE::Cluster ist Replicas nicht konfiguriert.",
        "es": "ALIYUN::MSE::Cluster no tiene Replicas configurado.",
        "fr": "ALIYUN::MSE::Cluster n'a pas Replicas configuré.",
        "pt": "ALIYUN::MSE::Cluster não tem Replicas configurado."
    },
    "recommendation": {
        "en": "Configure Replicas on ALIYUN::MSE::Cluster to satisfy the policy.",
        "zh": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::MSE::Cluster に Replicas を設定してください。",
        "de": "Konfigurieren Sie Replicas für ALIYUN::MSE::Cluster, um die Richtlinie zu erfüllen.",
        "es": "Configure Replicas en ALIYUN::MSE::Cluster para cumplir la política.",
        "fr": "Configurez Replicas sur ALIYUN::MSE::Cluster pour satisfaire la politique.",
        "pt": "Configure Replicas em ALIYUN::MSE::Cluster para atender à política."
    },
    "resource_types": ["ALIYUN::MSE::Cluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::MSE::Cluster")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Replicas"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Replicas")
}
