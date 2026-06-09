package infraguard.rules.aliyun.redis_instance_enabled_ssl

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "redis-instance-enabled-ssl",
	"severity": "medium",
	"name": {
		"en": "Redis Instance SSL Enabled",
		"zh": "Redis 实例开启 SSL 加密",
		"ja": "Redis インスタンスで SSL が有効",
		"de": "Redis-Instanz SSL aktiviert",
		"es": "SSL de Instancia Redis Habilitado",
		"fr": "SSL d'Instance Redis Activé",
		"pt": "SSL de Instância Redis Habilitado"
	},
	"description": {
		"en": "Ensures Redis instances have SSL encryption enabled.",
		"zh": "确保 Redis 实例开启了 SSL 加密。",
		"ja": "Redis インスタンスで SSL 暗号化が有効になっていることを確認します。",
		"de": "Stellt sicher, dass Redis-Instanzen SSL-Verschlüsselung aktiviert haben.",
		"es": "Garantiza que las instancias Redis tengan cifrado SSL habilitado.",
		"fr": "Garantit que les instances Redis ont le chiffrement SSL activé.",
		"pt": "Garante que as instâncias Redis tenham criptografia SSL habilitada."
	},
	"reason": {
		"en": "SSL encryption protects Redis data in transit from being intercepted.",
		"zh": "SSL 加密保护传输中的 Redis 数据不被截获。",
		"ja": "SSL 暗号化により、送信中の Redis データが傍受から保護されます。",
		"de": "SSL-Verschlüsselung schützt Redis-Daten während der Übertragung vor Abfangen.",
		"es": "El cifrado SSL protege los datos Redis en tránsito contra interceptación.",
		"fr": "Le chiffrement SSL protège les données Redis en transit contre l'interception.",
		"pt": "A criptografia SSL protege dados Redis em trânsito contra interceptação."
	},
	"recommendation": {
		"en": "Enable SSL for the Redis instance.",
		"zh": "为 Redis 实例开启 SSL 加密。",
		"ja": "Redis インスタンスで SSL を有効にします。",
		"de": "Aktivieren Sie SSL für die Redis-Instanz.",
		"es": "Habilite SSL para la instancia Redis.",
		"fr": "Activez SSL pour l'instance Redis.",
		"pt": "Habilite SSL para a instância Redis."
	},
	"resource_types": ["ALIYUN::REDIS::Instance"]
}

is_compliant(resource) if {
	ssl := helpers.get_property(resource, "SSLEnabled", "Disable")
	ssl == "Enable"
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SSLEnabled"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
