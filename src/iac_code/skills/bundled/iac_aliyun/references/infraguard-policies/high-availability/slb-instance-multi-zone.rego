package infraguard.rules.aliyun.slb_instance_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "slb-instance-multi-zone",
	"severity": "high",
	"name": {
		"en": "SLB Instance Multi-Zone Deployment",
		"zh": "SLB 实例多可用区部署",
		"ja": "SLB インスタンスのマルチゾーン展開",
		"de": "SLB-Instanz Multi-Zone-Bereitstellung",
		"es": "Implementación Multi-zona de instancia SLB",
		"fr": "Déploiement Multi-Zones de l'instance SLB",
		"pt": "Implantação Multi-zona de instância SLB",
	},
	"description": {
		"en": "SLB instances should configure a secondary zone for cross-zone failover.",
		"zh": "SLB 实例应配置备可用区，以支持跨可用区故障转移。",
		"ja": "SLB インスタンスは、クロスゾーンフェイルオーバーのためにセカンダリゾーンを設定する必要があります。",
		"de": "SLB-Instanzen sollten für zonenübergreifendes Failover eine sekundäre Zone konfigurieren.",
		"es": "Las instancias SLB deben configurar una zona secundaria para failover entre zonas.",
		"fr": "Les instances SLB doivent configurer une zone secondaire pour le basculement entre zones.",
		"pt": "Instâncias SLB devem configurar uma zona secundária para failover entre zonas.",
	},
	"reason": {
		"en": "The SLB instance does not have SlaveZoneId configured.",
		"zh": "SLB 实例未配置 SlaveZoneId。",
		"ja": "SLB インスタンスに SlaveZoneId が設定されていません。",
		"de": "Die SLB-Instanz hat SlaveZoneId nicht konfiguriert.",
		"es": "La instancia SLB no tiene configurado SlaveZoneId.",
		"fr": "L'instance SLB n'a pas SlaveZoneId configuré.",
		"pt": "A instância SLB não tem SlaveZoneId configurado.",
	},
	"recommendation": {
		"en": "Configure SlaveZoneId to enable multi-zone deployment.",
		"zh": "配置 SlaveZoneId 以启用多可用区部署。",
		"ja": "マルチゾーン展開を有効にするために SlaveZoneId を設定します。",
		"de": "Konfigurieren Sie SlaveZoneId, um Multi-Zone-Bereitstellung zu aktivieren.",
		"es": "Configure SlaveZoneId para habilitar la implementación multi-zona.",
		"fr": "Configurez SlaveZoneId pour activer le déploiement multi-zones.",
		"pt": "Configure SlaveZoneId para habilitar implantação multi-zona.",
	},
	"resource_types": ["ALIYUN::SLB::LoadBalancer"],
}

has_slave_zone(resource) if {
	object.get(resource.Properties, "SlaveZoneId", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
	not has_slave_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SlaveZoneId"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
