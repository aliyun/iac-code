package infraguard.rules.aliyun.ack_cluster_node_pool_autoscaling_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ack-cluster-node-pool-autoscaling-enabled",
    "severity": "high",
    "name": {
        "en": "ACK cluster must configure worker VSwitches",
        "zh": "ACK 集群必须配置工作节点交换机",
        "ja": "ACK 集群必须配置工作节点交换机",
        "de": "ACK 集群必须配置工作节点交换机",
        "es": "ACK 集群必须配置工作节点交换机",
        "fr": "ACK 集群必须配置工作节点交换机",
        "pt": "ACK 集群必须配置工作节点交换机"
    },
    "description": {
        "en": "Checks ACK cluster must configure worker VSwitches",
        "zh": "检查ACK 集群必须配置工作节点交换机",
        "ja": "检查ACK 集群必须配置工作节点交换机",
        "de": "检查ACK 集群必须配置工作节点交换机",
        "es": "检查ACK 集群必须配置工作节点交换机",
        "fr": "检查ACK 集群必须配置工作节点交换机",
        "pt": "检查ACK 集群必须配置工作节点交换机"
    },
    "reason": {
        "en": "ACK cluster must configure worker VSwitches is not satisfied.",
        "zh": "ACK 集群必须配置工作节点交换机未满足。",
        "ja": "ACK 集群必须配置工作节点交换机未满足。",
        "de": "ACK 集群必须配置工作节点交换机未满足。",
        "es": "ACK 集群必须配置工作节点交换机未满足。",
        "fr": "ACK 集群必须配置工作节点交换机未满足。",
        "pt": "ACK 集群必须配置工作节点交换机未满足。"
    },
    "recommendation": {
        "en": "Configure WorkerVSwitchIds on ALIYUN::CS::ClusterApplication to satisfy the policy.",
        "zh": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "ja": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "de": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "es": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "fr": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。",
        "pt": "请在 ALIYUN::CS::ClusterApplication 上配置 WorkerVSwitchIds 以满足策略。"
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
