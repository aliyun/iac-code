package infraguard.rules.aliyun.rds_instance_secondary_zone_required

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "rds-instance-secondary-zone-required",
	"severity": "high",
	"name": {
		"en": "RDS Instance Secondary Zone Required",
		"zh": "RDS 实例必须配置备用可用区",
		"ja": "RDS インスタンスのセカンダリゾーン必須",
		"de": "Sekundäre Zone für RDS-Instanz erforderlich",
		"es": "Zona secundaria requerida para instancia RDS",
		"fr": "Zone secondaire requise pour l'instance RDS",
		"pt": "Zona secundária obrigatória para instância RDS",
	},
	"description": {
		"en": "RDS high-availability deployments should place the secondary instance in another zone.",
		"zh": "RDS 高可用部署应将备实例放置在另一个可用区。",
		"ja": "RDS 高可用性展開では、セカンダリインスタンスを別のゾーンに配置する必要があります。",
		"de": "RDS-Hochverfügbarkeitsbereitstellungen sollten die sekundäre Instanz in einer anderen Zone platzieren.",
		"es": "Las implementaciones RDS de alta disponibilidad deben ubicar la instancia secundaria en otra zona.",
		"fr": "Les déploiements RDS haute disponibilité doivent placer l'instance secondaire dans une autre zone.",
		"pt": "Implantações RDS de alta disponibilidade devem colocar a instância secundária em outra zona.",
	},
	"reason": {
		"en": "The RDS instance does not specify ZoneIdSlave1.",
		"zh": "RDS 实例未指定 ZoneIdSlave1。",
		"ja": "RDS インスタンスで ZoneIdSlave1 が指定されていません。",
		"de": "Die RDS-Instanz gibt ZoneIdSlave1 nicht an.",
		"es": "La instancia RDS no especifica ZoneIdSlave1.",
		"fr": "L'instance RDS ne spécifie pas ZoneIdSlave1.",
		"pt": "A instância RDS não especifica ZoneIdSlave1.",
	},
	"recommendation": {
		"en": "Configure ZoneIdSlave1 for cross-zone high availability.",
		"zh": "配置 ZoneIdSlave1 以支持跨可用区高可用。",
		"ja": "クロスゾーン高可用性のために ZoneIdSlave1 を設定します。",
		"de": "Konfigurieren Sie ZoneIdSlave1 für zonenübergreifende Hochverfügbarkeit.",
		"es": "Configure ZoneIdSlave1 para alta disponibilidad entre zonas.",
		"fr": "Configurez ZoneIdSlave1 pour la haute disponibilité entre zones.",
		"pt": "Configure ZoneIdSlave1 para alta disponibilidade entre zonas.",
	},
	"resource_types": ["ALIYUN::RDS::DBInstance"],
}

has_secondary_zone(resource) if {
	object.get(resource.Properties, "ZoneIdSlave1", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
	not has_secondary_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "ZoneIdSlave1"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
