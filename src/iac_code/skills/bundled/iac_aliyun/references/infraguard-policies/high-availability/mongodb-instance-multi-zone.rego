package infraguard.rules.aliyun.mongodb_instance_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "mongodb-instance-multi-zone",
	"severity": "medium",
	"name": {
		"en": "MongoDB Instance Multi-Zone Deployment",
		"zh": "MongoDB 实例多可用区部署",
		"ja": "MongoDB インスタンスのマルチゾーン展開",
		"de": "MongoDB-Instanz Multi-Zone-Bereitstellung",
		"es": "Despliegue Multi-zona de instancia MongoDB",
		"fr": "Déploiement Multi-Zones d'instance MongoDB",
		"pt": "Implantação Multi-zona de instância MongoDB",
	},
	"description": {
		"en": "MongoDB instances should configure a secondary or hidden zone for high availability.",
		"zh": "MongoDB 实例应配置备用可用区或隐藏可用区以实现高可用。",
		"ja": "MongoDB インスタンスは高可用性のためにセカンダリゾーンまたは隠しゾーンを設定する必要があります。",
		"de": "MongoDB-Instanzen sollten für Hochverfügbarkeit eine sekundäre oder versteckte Zone konfigurieren.",
		"es": "Las instancias MongoDB deben configurar una zona secundaria u oculta para alta disponibilidad.",
		"fr": "Les instances MongoDB doivent configurer une zone secondaire ou cachée pour la haute disponibilité.",
		"pt": "Instâncias MongoDB devem configurar uma zona secundária ou oculta para alta disponibilidade.",
	},
	"reason": {
		"en": "The MongoDB instance does not have a secondary or hidden zone configured.",
		"zh": "MongoDB 实例未配置备用可用区或隐藏可用区。",
		"ja": "MongoDB インスタンスにセカンダリゾーンまたは隠しゾーンが設定されていません。",
		"de": "Die MongoDB-Instanz hat keine sekundäre oder versteckte Zone konfiguriert.",
		"es": "La instancia MongoDB no tiene configurada una zona secundaria u oculta.",
		"fr": "L'instance MongoDB n'a pas de zone secondaire ou cachée configurée.",
		"pt": "A instância MongoDB não tem uma zona secundária ou oculta configurada.",
	},
	"recommendation": {
		"en": "Configure SecondaryZoneId or HiddenZoneId to enable multi-zone deployment.",
		"zh": "配置 SecondaryZoneId 或 HiddenZoneId 以启用多可用区部署。",
		"ja": "SecondaryZoneId または HiddenZoneId を設定してマルチゾーン展開を有効にします。",
		"de": "Konfigurieren Sie SecondaryZoneId oder HiddenZoneId, um Multi-Zone-Bereitstellung zu aktivieren.",
		"es": "Configure SecondaryZoneId o HiddenZoneId para habilitar el despliegue multi-zona.",
		"fr": "Configurez SecondaryZoneId ou HiddenZoneId pour activer le déploiement multi-zones.",
		"pt": "Configure SecondaryZoneId ou HiddenZoneId para habilitar implantação multi-zona.",
	},
	"resource_types": ["ALIYUN::MONGODB::Instance"],
}

is_multi_zone(resource) if {
	object.get(resource.Properties, "SecondaryZoneId", "") != ""
}

is_multi_zone(resource) if {
	object.get(resource.Properties, "HiddenZoneId", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::MONGODB::Instance")
	not is_multi_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
