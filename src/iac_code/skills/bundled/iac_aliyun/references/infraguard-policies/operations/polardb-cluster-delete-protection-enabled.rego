package infraguard.rules.aliyun.polardb_cluster_delete_protection_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "polardb-cluster-delete-protection-enabled",
    "severity": "medium",
    "name": {
        "en": "PolarDB cluster must enable deletion protection",
        "zh": "PolarDB 集群必须启用删除保护",
        "ja": "ALIYUN::POLARDB::DBCluster には DeletionProtection を設定する必要があります",
        "de": "Für ALIYUN::POLARDB::DBCluster muss DeletionProtection konfiguriert sein",
        "es": "ALIYUN::POLARDB::DBCluster debe tener DeletionProtection configurado",
        "fr": "ALIYUN::POLARDB::DBCluster doit avoir DeletionProtection configuré",
        "pt": "ALIYUN::POLARDB::DBCluster deve ter DeletionProtection configurado"
    },
    "description": {
        "en": "Checks PolarDB cluster must enable deletion protection",
        "zh": "检查PolarDB 集群必须启用删除保护",
        "ja": "ALIYUN::POLARDB::DBCluster に DeletionProtection が設定されていることを確認します",
        "de": "Prüft, ob DeletionProtection für ALIYUN::POLARDB::DBCluster konfiguriert ist",
        "es": "Comprueba que ALIYUN::POLARDB::DBCluster tenga DeletionProtection configurado",
        "fr": "Vérifie que ALIYUN::POLARDB::DBCluster a DeletionProtection configuré",
        "pt": "Verifica se ALIYUN::POLARDB::DBCluster tem DeletionProtection configurado"
    },
    "reason": {
        "en": "PolarDB cluster must enable deletion protection is not satisfied.",
        "zh": "PolarDB 集群必须启用删除保护未满足。",
        "ja": "ALIYUN::POLARDB::DBCluster に DeletionProtection が設定されていません。",
        "de": "Für ALIYUN::POLARDB::DBCluster ist DeletionProtection nicht konfiguriert.",
        "es": "ALIYUN::POLARDB::DBCluster no tiene DeletionProtection configurado.",
        "fr": "ALIYUN::POLARDB::DBCluster n'a pas DeletionProtection configuré.",
        "pt": "ALIYUN::POLARDB::DBCluster não tem DeletionProtection configurado."
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::POLARDB::DBCluster to satisfy the policy.",
        "zh": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::POLARDB::DBCluster に DeletionProtection を設定してください。",
        "de": "Konfigurieren Sie DeletionProtection für ALIYUN::POLARDB::DBCluster, um die Richtlinie zu erfüllen.",
        "es": "Configure DeletionProtection en ALIYUN::POLARDB::DBCluster para cumplir la política.",
        "fr": "Configurez DeletionProtection sur ALIYUN::POLARDB::DBCluster pour satisfaire la politique.",
        "pt": "Configure DeletionProtection em ALIYUN::POLARDB::DBCluster para atender à política."
    },
    "resource_types": ["ALIYUN::POLARDB::DBCluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::POLARDB::DBCluster")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DeletionProtection"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "DeletionProtection", false) == true
}
