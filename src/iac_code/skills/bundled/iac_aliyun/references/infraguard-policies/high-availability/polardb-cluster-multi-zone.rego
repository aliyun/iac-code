package infraguard.rules.aliyun.polardb_cluster_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "polardb-cluster-multi-zone",
	"severity": "medium",
	"name": {
		"en": "PolarDB Cluster Multi-Zone Deployment",
		"zh": "PolarDB 集群多可用区部署",
		"ja": "PolarDB クラスタのマルチゾーン展開",
		"de": "PolarDB-Cluster Multi-Zone-Bereitstellung",
		"es": "Implementación Multi-zona de clúster PolarDB",
		"fr": "Déploiement Multi-Zones du cluster PolarDB",
		"pt": "Implantação Multi-zona de cluster PolarDB",
	},
	"description": {
		"en": "PolarDB clusters should configure a standby availability zone for zone-level failover.",
		"zh": "PolarDB 集群应配置备用可用区，以支持可用区级故障转移。",
		"ja": "PolarDB クラスタは、ゾーンレベルのフェイルオーバーのためにスタンバイ可用性ゾーンを設定する必要があります。",
		"de": "PolarDB-Cluster sollten eine Standby-Verfügbarkeitszone für zonenbezogenes Failover konfigurieren.",
		"es": "Los clústeres PolarDB deben configurar una zona de disponibilidad en espera para la conmutación por error a nivel de zona.",
		"fr": "Les clusters PolarDB doivent configurer une zone de disponibilité de secours pour le basculement au niveau de la zone.",
		"pt": "Clusters PolarDB devem configurar uma zona de disponibilidade em espera para failover no nível da zona.",
	},
	"reason": {
		"en": "The PolarDB cluster does not have a standby availability zone configured.",
		"zh": "PolarDB 集群未配置备用可用区。",
		"ja": "PolarDB クラスタにスタンバイ可用性ゾーンが設定されていません。",
		"de": "Der PolarDB-Cluster hat keine Standby-Verfügbarkeitszone konfiguriert.",
		"es": "El clúster PolarDB no tiene configurada una zona de disponibilidad en espera.",
		"fr": "Le cluster PolarDB n'a pas de zone de disponibilité de secours configurée.",
		"pt": "O cluster PolarDB não tem uma zona de disponibilidade em espera configurada.",
	},
	"recommendation": {
		"en": "Configure StandbyAZ to enable multi-zone deployment.",
		"zh": "配置 StandbyAZ 以启用多可用区部署。",
		"ja": "マルチゾーン展開を有効にするために StandbyAZ を設定します。",
		"de": "Konfigurieren Sie StandbyAZ, um Multi-Zone-Bereitstellung zu aktivieren.",
		"es": "Configure StandbyAZ para habilitar la implementación multi-zona.",
		"fr": "Configurez StandbyAZ pour activer le déploiement multi-zones.",
		"pt": "Configure StandbyAZ para habilitar implantação multi-zona.",
	},
	"resource_types": ["ALIYUN::POLARDB::DBCluster"],
}

is_multi_zone(resource) if {
	object.get(resource.Properties, "StandbyAZ", "") != ""
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::POLARDB::DBCluster")
	not is_multi_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "StandbyAZ"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
