package infraguard.rules.aliyun.slb_instance_master_zone_required

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "slb-instance-master-zone-required",
	"severity": "medium",
	"name": {
		"en": "SLB Instance Master Zone Required",
		"zh": "SLB 实例必须配置主可用区",
		"ja": "SLB インスタンスのマスターゾーン必須",
		"de": "Master-Zone für SLB-Instanz erforderlich",
		"es": "Zona maestra requerida para instancia SLB",
		"fr": "Zone maître requise pour l'instance SLB",
		"pt": "Zona mestre obrigatória para instância SLB",
	},
	"description": {
		"en": "SLB instances should configure a master zone as part of primary and secondary zone deployment.",
		"zh": "SLB 实例应配置主可用区，作为主备可用区部署的一部分。",
		"ja": "SLB インスタンスは、プライマリ/セカンダリゾーン展開の一部としてマスターゾーンを設定する必要があります。",
		"de": "SLB-Instanzen sollten als Teil der primären und sekundären Zonenbereitstellung eine Master-Zone konfigurieren.",
		"es": "Las instancias SLB deben configurar una zona maestra como parte del despliegue de zonas primaria y secundaria.",
		"fr": "Les instances SLB doivent configurer une zone maître dans le cadre du déploiement des zones principale et secondaire.",
		"pt": "Instâncias SLB devem configurar uma zona mestre como parte da implantação de zonas primária e secundária.",
	},
	"reason": {
		"en": "The SLB instance does not specify MasterZoneId.",
		"zh": "SLB 实例未指定 MasterZoneId。",
		"ja": "SLB インスタンスで MasterZoneId が指定されていません。",
		"de": "Die SLB-Instanz gibt MasterZoneId nicht an.",
		"es": "La instancia SLB no especifica MasterZoneId.",
		"fr": "L'instance SLB ne spécifie pas MasterZoneId.",
		"pt": "A instância SLB não especifica MasterZoneId.",
	},
	"recommendation": {
		"en": "Configure MasterZoneId on the SLB instance.",
		"zh": "在 SLB 实例上配置 MasterZoneId。",
		"ja": "SLB インスタンスに MasterZoneId を設定します。",
		"de": "Konfigurieren Sie MasterZoneId für die SLB-Instanz.",
		"es": "Configure MasterZoneId en la instancia SLB.",
		"fr": "Configurez MasterZoneId sur l'instance SLB.",
		"pt": "Configure MasterZoneId na instância SLB.",
	},
	"resource_types": ["ALIYUN::SLB::LoadBalancer"],
}

has_master_zone(resource) if {
	object.get(resource.Properties, "MasterZoneId", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
	not has_master_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "MasterZoneId"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
