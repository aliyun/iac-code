package infraguard.rules.aliyun.polardb_cluster_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "polardb-cluster-tags-required",
    "severity": "medium",
    "name": {
        "en": "PolarDB cluster must configure tags",
        "zh": "PolarDB 集群必须配置标签",
        "ja": "ALIYUN::POLARDB::DBCluster には Tags を設定する必要があります",
        "de": "Für ALIYUN::POLARDB::DBCluster muss Tags konfiguriert sein",
        "es": "ALIYUN::POLARDB::DBCluster debe tener Tags configurado",
        "fr": "ALIYUN::POLARDB::DBCluster doit avoir Tags configuré",
        "pt": "ALIYUN::POLARDB::DBCluster deve ter Tags configurado"
    },
    "description": {
        "en": "Checks PolarDB cluster must configure tags",
        "zh": "检查PolarDB 集群必须配置标签",
        "ja": "ALIYUN::POLARDB::DBCluster に Tags が設定されていることを確認します",
        "de": "Prüft, ob Tags für ALIYUN::POLARDB::DBCluster konfiguriert ist",
        "es": "Comprueba que ALIYUN::POLARDB::DBCluster tenga Tags configurado",
        "fr": "Vérifie que ALIYUN::POLARDB::DBCluster a Tags configuré",
        "pt": "Verifica se ALIYUN::POLARDB::DBCluster tem Tags configurado"
    },
    "reason": {
        "en": "PolarDB cluster must configure tags is not satisfied.",
        "zh": "PolarDB 集群必须配置标签未满足。",
        "ja": "ALIYUN::POLARDB::DBCluster に Tags が設定されていません。",
        "de": "Für ALIYUN::POLARDB::DBCluster ist Tags nicht konfiguriert.",
        "es": "ALIYUN::POLARDB::DBCluster no tiene Tags configurado.",
        "fr": "ALIYUN::POLARDB::DBCluster n'a pas Tags configuré.",
        "pt": "ALIYUN::POLARDB::DBCluster não tem Tags configurado."
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::POLARDB::DBCluster to satisfy the policy.",
        "zh": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::POLARDB::DBCluster に Tags を設定してください。",
        "de": "Konfigurieren Sie Tags für ALIYUN::POLARDB::DBCluster, um die Richtlinie zu erfüllen.",
        "es": "Configure Tags en ALIYUN::POLARDB::DBCluster para cumplir la política.",
        "fr": "Configurez Tags sur ALIYUN::POLARDB::DBCluster pour satisfaire la politique.",
        "pt": "Configure Tags em ALIYUN::POLARDB::DBCluster para atender à política."
    },
    "resource_types": ["ALIYUN::POLARDB::DBCluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::POLARDB::DBCluster")
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
