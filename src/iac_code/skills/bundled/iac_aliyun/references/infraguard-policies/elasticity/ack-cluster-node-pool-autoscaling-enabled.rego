package infraguard.rules.aliyun.ack_cluster_node_pool_autoscaling_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ack-cluster-node-pool-autoscaling-enabled",
    "severity": "high",
    "name": {
        "en": "ACK cluster must configure worker VSwitches",
        "zh": "ACK 集群必须配置工作节点交换机",
        "ja": "ALIYUN::CS::ClusterApplication には WorkerVSwitchIds を設定する必要があります",
        "de": "Für ALIYUN::CS::ClusterApplication muss WorkerVSwitchIds konfiguriert sein",
        "es": "ALIYUN::CS::ClusterApplication debe tener WorkerVSwitchIds configurado",
        "fr": "ALIYUN::CS::ClusterApplication doit avoir WorkerVSwitchIds configuré",
        "pt": "ALIYUN::CS::ClusterApplication deve ter WorkerVSwitchIds configurado"
    },
    "description": {
        "en": "Checks ACK cluster must configure worker VSwitches",
        "zh": "检查ACK 集群必须配置工作节点交换机",
        "ja": "ALIYUN::CS::ClusterApplication に WorkerVSwitchIds が設定されていることを確認します",
        "de": "Prüft, ob WorkerVSwitchIds für ALIYUN::CS::ClusterApplication konfiguriert ist",
        "es": "Comprueba que ALIYUN::CS::ClusterApplication tenga WorkerVSwitchIds configurado",
        "fr": "Vérifie que ALIYUN::CS::ClusterApplication a WorkerVSwitchIds configuré",
        "pt": "Verifica se ALIYUN::CS::ClusterApplication tem WorkerVSwitchIds configurado"
    },
    "reason": {
        "en": "ACK cluster must configure worker VSwitches is not satisfied.",
        "zh": "ACK 集群必须配置工作节点交换机未满足。",
        "ja": "ALIYUN::CS::ClusterApplication に WorkerVSwitchIds が設定されていません。",
        "de": "Für ALIYUN::CS::ClusterApplication ist WorkerVSwitchIds nicht konfiguriert.",
        "es": "ALIYUN::CS::ClusterApplication no tiene WorkerVSwitchIds configurado.",
        "fr": "ALIYUN::CS::ClusterApplication n'a pas WorkerVSwitchIds configuré.",
        "pt": "ALIYUN::CS::ClusterApplication não tem WorkerVSwitchIds configurado."
    },
    "recommendation": {
        "en": "Configure WorkerVSwitchIds on ALIYUN::CS::ClusterApplication to satisfy the policy.",
        "zh": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::CS::ClusterApplication に WorkerVSwitchIds を設定してください。",
        "de": "Konfigurieren Sie WorkerVSwitchIds für ALIYUN::CS::ClusterApplication, um die Richtlinie zu erfüllen.",
        "es": "Configure WorkerVSwitchIds en ALIYUN::CS::ClusterApplication para cumplir la política.",
        "fr": "Configurez WorkerVSwitchIds sur ALIYUN::CS::ClusterApplication pour satisfaire la politique.",
        "pt": "Configure WorkerVSwitchIds em ALIYUN::CS::ClusterApplication para atender à política."
    },
    "resource_types": ["ALIYUN::CS::ClusterApplication"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CS::ClusterApplication")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "WorkerVSwitchIds"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "WorkerVSwitchIds")
}
