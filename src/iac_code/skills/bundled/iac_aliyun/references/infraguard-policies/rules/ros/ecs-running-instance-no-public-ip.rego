package infraguard.rules.aliyun.ecs_running_instance_no_public_ip

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ecs-running-instance-no-public-ip",
	"severity": "high",
	"name": {
		"en": "ECS Instance No Public IP",
		"zh": "ECS 实例不分配公网 IP",
		"ja": "ECS インスタンスにパブリック IP がない",
		"de": "ECS-Instanz Keine öffentliche IP",
		"es": "Instancia ECS Sin IP Pública",
		"fr": "Instance ECS Sans IP Publique",
		"pt": "Instância ECS Sem IP Público"
	},
	"description": {
		"en": "ECS instances should not have a public IP address to reduce direct internet exposure.",
		"zh": "ECS 实例不应分配公网 IP，以减少直接暴露在互联网上的风险。",
		"ja": "ECS インスタンスは、直接的なインターネットへの露出を減らすためにパブリック IP アドレスを持つべきではありません。",
		"de": "ECS-Instanzen sollten keine öffentliche IP-Adresse haben, um die direkte Internetexposition zu reduzieren.",
		"es": "Las instancias ECS no deben tener una dirección IP pública para reducir la exposición directa a Internet.",
		"fr": "Les instances ECS ne doivent pas avoir d'adresse IP publique pour réduire l'exposition directe à Internet.",
		"pt": "As instâncias ECS não devem ter um endereço IP público para reduzir a exposição direta à Internet."
	},
	"reason": {
		"en": "Public IP addresses allow direct access from the internet, increasing the attack surface.",
		"zh": "分配公网 IP 会使实例直接暴露在互联网上，增加了攻击面。",
		"ja": "パブリック IP アドレスはインターネットからの直接アクセスを許可し、攻撃面を増加させます。",
		"de": "Öffentliche IP-Adressen ermöglichen direkten Zugriff aus dem Internet und erhöhen die Angriffsfläche.",
		"es": "Las direcciones IP públicas permiten acceso directo desde Internet, aumentando la superficie de ataque.",
		"fr": "Les adresses IP publiques permettent un accès direct depuis Internet, augmentant la surface d'attaque.",
		"pt": "Endereços IP públicos permitem acesso direto da Internet, aumentando a superfície de ataque."
	},
	"recommendation": {
		"en": "Remove public IP assignment by setting AllocatePublicIP to false or using a NAT gateway for egress.",
		"zh": "通过将 AllocatePublicIP 设置为 false 或使用 NAT 网关来取消分配公网 IP。",
		"ja": "AllocatePublicIP を false に設定するか、エグレスに NAT ゲートウェイを使用してパブリック IP の割り当てを削除します。",
		"de": "Entfernen Sie die öffentliche IP-Zuweisung, indem Sie AllocatePublicIP auf false setzen oder ein NAT-Gateway für den Ausgang verwenden.",
		"es": "Elimine la asignación de IP pública estableciendo AllocatePublicIP en false o usando una puerta de enlace NAT para la salida.",
		"fr": "Supprimez l'attribution d'IP publique en définissant AllocatePublicIP sur false ou en utilisant une passerelle NAT pour la sortie.",
		"pt": "Remova a atribuição de IP público definindo AllocatePublicIP como false ou usando um gateway NAT para saída."
	},
	"resource_types": ["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"]
}

# Check if the instance has a public IP allocated
has_public_ip(resource) if {
	helpers.get_property(resource, "AllocatePublicIP", false) == true
}

deny contains result if {
	some name, resource in helpers.resources_by_types(rule_meta.resource_types)
	has_public_ip(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "AllocatePublicIP"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
