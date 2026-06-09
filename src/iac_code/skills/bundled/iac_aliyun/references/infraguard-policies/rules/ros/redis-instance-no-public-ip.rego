package infraguard.rules.aliyun.redis_instance_no_public_ip

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "redis-instance-no-public-ip",
	"severity": "high",
	"name": {
		"en": "Redis Instance No Public IP",
		"zh": "Redis 实例未设置公网 IP",
		"ja": "Redis インスタンスにパブリック IP なし",
		"de": "Redis-Instanz ohne öffentliche IP",
		"es": "Instancia Redis Sin IP Pública",
		"fr": "Instance Redis Sans IP Publique",
		"pt": "Instância Redis Sem IP Público"
	},
	"description": {
		"en": "Ensures Redis instance does not have public IP assigned.",
		"zh": "确保 Redis 实例未设置公网 IP。",
		"ja": "Redis インスタンスにパブリック IP が割り当てられていないことを確認します。",
		"de": "Stellt sicher, dass der Redis-Instanz keine öffentliche IP zugewiesen ist.",
		"es": "Garantiza que la instancia Redis no tenga una IP pública asignada.",
		"fr": "Garantit que l'instance Redis n'a pas d'IP publique assignée.",
		"pt": "Garante que a instância Redis não tenha IP público atribuído."
	},
	"reason": {
		"en": "Public IP exposes Redis instance to internet attacks.",
		"zh": "公网 IP 使 Redis 实例暴露于互联网攻击。",
		"ja": "パブリック IP により、Redis インスタンスがインターネット攻撃にさらされます。",
		"de": "Öffentliche IP setzt die Redis-Instanz Internetangriffen aus.",
		"es": "La IP pública expone la instancia Redis a ataques de internet.",
		"fr": "L'IP publique expose l'instance Redis aux attaques Internet.",
		"pt": "O IP público expõe a instância Redis a ataques da internet."
	},
	"recommendation": {
		"en": "Remove public IP from the Redis instance.",
		"zh": "移除 Redis 实例的公网 IP。",
		"ja": "Redis インスタンスからパブリック IP を削除します。",
		"de": "Entfernen Sie die öffentliche IP von der Redis-Instanz.",
		"es": "Elimine la IP pública de la instancia Redis.",
		"fr": "Supprimez l'IP publique de l'instance Redis.",
		"pt": "Remova o IP público da instância Redis."
	},
	"resource_types": ["ALIYUN::REDIS::Instance"]
}

is_compliant(resource) if {
	connections := helpers.get_property(resource, "Connections", {})
	object.get(connections, "PublicConnection", null) == null
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "Connections", "PublicConnection"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
