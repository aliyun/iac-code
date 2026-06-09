package infraguard.rules.aliyun.security_group_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-group-vpc-required",
    "severity": "high",
    "name": {
        "en": "Security group must bind VPC",
        "zh": "安全组必须绑定 VPC",
        "ja": "ALIYUN::ECS::SecurityGroup には VpcId を設定する必要があります",
        "de": "Für ALIYUN::ECS::SecurityGroup muss VpcId konfiguriert sein",
        "es": "ALIYUN::ECS::SecurityGroup debe tener VpcId configurado",
        "fr": "ALIYUN::ECS::SecurityGroup doit avoir VpcId configuré",
        "pt": "ALIYUN::ECS::SecurityGroup deve ter VpcId configurado"
    },
    "description": {
        "en": "Checks Security group must bind VPC",
        "zh": "检查安全组必须绑定 VPC",
        "ja": "ALIYUN::ECS::SecurityGroup に VpcId が設定されていることを確認します",
        "de": "Prüft, ob VpcId für ALIYUN::ECS::SecurityGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::SecurityGroup tenga VpcId configurado",
        "fr": "Vérifie que ALIYUN::ECS::SecurityGroup a VpcId configuré",
        "pt": "Verifica se ALIYUN::ECS::SecurityGroup tem VpcId configurado"
    },
    "reason": {
        "en": "Security group must bind VPC is not satisfied.",
        "zh": "安全组必须绑定 VPC未满足。",
        "ja": "ALIYUN::ECS::SecurityGroup に VpcId が設定されていません。",
        "de": "Für ALIYUN::ECS::SecurityGroup ist VpcId nicht konfiguriert.",
        "es": "ALIYUN::ECS::SecurityGroup no tiene VpcId configurado.",
        "fr": "ALIYUN::ECS::SecurityGroup n'a pas VpcId configuré.",
        "pt": "ALIYUN::ECS::SecurityGroup não tem VpcId configurado."
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::SecurityGroup に VpcId を設定してください。",
        "de": "Konfigurieren Sie VpcId für ALIYUN::ECS::SecurityGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure VpcId en ALIYUN::ECS::SecurityGroup para cumplir la política.",
        "fr": "Configurez VpcId sur ALIYUN::ECS::SecurityGroup pour satisfaire la politique.",
        "pt": "Configure VpcId em ALIYUN::ECS::SecurityGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VpcId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VpcId")
}
