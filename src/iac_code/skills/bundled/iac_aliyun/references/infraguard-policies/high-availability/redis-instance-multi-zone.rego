package infraguard.rules.aliyun.redis_instance_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "redis-instance-multi-zone",
	"severity": "medium",
	"name": {
		"en": "Redis Instance Multi-Zone Deployment",
		"zh": "Redis 实例多可用区部署",
		"ja": "Redis インスタンスのマルチゾーン展開",
		"de": "Redis-Instanz Multi-Zone-Bereitstellung",
		"es": "Despliegue Multi-zona de instancia Redis",
		"fr": "Déploiement Multi-Zones d'instance Redis",
		"pt": "Implantação Multi-zona de instância Redis",
	},
	"description": {
		"en": "Redis instances should configure a secondary zone for high availability.",
		"zh": "Redis 实例应配置备用可用区以实现高可用。",
		"ja": "Redis インスタンスは高可用性のためにセカンダリゾーンを設定する必要があります。",
		"de": "Redis-Instanzen sollten für Hochverfügbarkeit eine sekundäre Zone konfigurieren.",
		"es": "Las instancias Redis deben configurar una zona secundaria para alta disponibilidad.",
		"fr": "Les instances Redis doivent configurer une zone secondaire pour la haute disponibilité.",
		"pt": "Instâncias Redis devem configurar uma zona secundária para alta disponibilidade.",
	},
	"reason": {
		"en": "The Redis instance does not have a secondary zone configured.",
		"zh": "Redis 实例未配置备用可用区。",
		"ja": "Redis インスタンスにセカンダリゾーンが設定されていません。",
		"de": "Die Redis-Instanz hat keine sekundäre Zone konfiguriert.",
		"es": "La instancia Redis no tiene configurada una zona secundaria.",
		"fr": "L'instance Redis n'a pas de zone secondaire configurée.",
		"pt": "A instância Redis não tem uma zona secundária configurada.",
	},
	"recommendation": {
		"en": "Configure SecondaryZoneId to enable multi-zone deployment.",
		"zh": "配置 SecondaryZoneId 以启用多可用区部署。",
		"ja": "マルチゾーン展開を有効にするために SecondaryZoneId を設定します。",
		"de": "Konfigurieren Sie SecondaryZoneId, um Multi-Zone-Bereitstellung zu aktivieren.",
		"es": "Configure SecondaryZoneId para habilitar la implementación multi-zona.",
		"fr": "Configurez SecondaryZoneId pour activer le déploiement multi-zones.",
		"pt": "Configure SecondaryZoneId para habilitar implantação multi-zona.",
	},
	"resource_types": ["ALIYUN::REDIS::Instance"],
}

is_multi_zone(resource) if {
	object.get(resource.Properties, "SecondaryZoneId", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
	not is_multi_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SecondaryZoneId"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
