package infraguard.rules.aliyun.rds_instance_zone_required

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "rds-instance-zone-required",
	"severity": "medium",
	"name": {
		"en": "RDS Instance Primary Zone Required",
		"zh": "RDS 实例必须配置主可用区",
		"ja": "RDS インスタンスのプライマリゾーン必須",
		"de": "Primäre Zone für RDS-Instanz erforderlich",
		"es": "Zona primaria requerida para instancia RDS",
		"fr": "Zone principale requise pour l'instance RDS",
		"pt": "Zona primária obrigatória para instância RDS",
	},
	"description": {
		"en": "RDS instances should explicitly configure the primary zone used for placement and failover planning.",
		"zh": "RDS 实例应显式配置主可用区，用于资源放置和故障转移规划。",
		"ja": "RDS インスタンスは、配置とフェイルオーバー計画に使用するプライマリゾーンを明示的に設定する必要があります。",
		"de": "RDS-Instanzen sollten die primäre Zone für Platzierung und Failover-Planung explizit konfigurieren.",
		"es": "Las instancias RDS deben configurar explícitamente la zona primaria usada para ubicación y planificación de failover.",
		"fr": "Les instances RDS doivent configurer explicitement la zone principale utilisée pour le placement et la planification du basculement.",
		"pt": "Instâncias RDS devem configurar explicitamente a zona primária usada para posicionamento e planejamento de failover.",
	},
	"reason": {
		"en": "The RDS instance does not specify ZoneId.",
		"zh": "RDS 实例未指定 ZoneId。",
		"ja": "RDS インスタンスで ZoneId が指定されていません。",
		"de": "Die RDS-Instanz gibt ZoneId nicht an.",
		"es": "La instancia RDS no especifica ZoneId.",
		"fr": "L'instance RDS ne spécifie pas ZoneId.",
		"pt": "A instância RDS não especifica ZoneId.",
	},
	"recommendation": {
		"en": "Configure ZoneId on the RDS instance.",
		"zh": "在 RDS 实例上配置 ZoneId。",
		"ja": "RDS インスタンスに ZoneId を設定します。",
		"de": "Konfigurieren Sie ZoneId für die RDS-Instanz.",
		"es": "Configure ZoneId en la instancia RDS.",
		"fr": "Configurez ZoneId sur l'instance RDS.",
		"pt": "Configure ZoneId na instância RDS.",
	},
	"resource_types": ["ALIYUN::RDS::DBInstance"],
}

has_zone(resource) if {
	object.get(resource.Properties, "ZoneId", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
	not has_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "ZoneId"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
