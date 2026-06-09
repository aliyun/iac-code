package infraguard.rules.aliyun.alb_instance_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "alb-instance-multi-zone",
	"severity": "high",
	"name": {
		"en": "ALB Instance Multi-Zone Deployment",
		"zh": "使用多可用区的 ALB 实例",
		"ja": "ALB インスタンスのマルチゾーン展開",
		"de": "ALB-Instanz Multi-Zone-Bereitstellung",
		"es": "Despliegue Multi-zona de Instancia ALB",
		"fr": "Déploiement Multi-Zones de l'Instance ALB",
		"pt": "Implantação Multi-zona da Instância ALB",
	},
	"description": {
		"en": "ALB instances should be deployed across multiple availability zones for high availability and real-time disaster recovery.",
		"zh": "ALB 实例应部署在多个可用区，以实现高可用和实时容灾。",
		"ja": "ALB インスタンスは、高可用性とリアルタイム災害復旧のために複数の可用性ゾーンに展開する必要があります。",
		"de": "ALB-Instanzen sollten für Hochverfügbarkeit und Disaster Recovery in Echtzeit über mehrere Verfügbarkeitszonen bereitgestellt werden.",
		"es": "Las instancias ALB deben implementarse en múltiples zonas de disponibilidad para alta disponibilidad y recuperación ante desastres en tiempo real.",
		"fr": "Les instances ALB doivent être déployées sur plusieurs zones de disponibilité pour la haute disponibilité et la reprise après sinistre en temps réel.",
		"pt": "Instâncias ALB devem ser implantadas em múltiplas zonas de disponibilidade para alta disponibilidade e recuperação de desastres em tempo real.",
	},
	"reason": {
		"en": "The ALB instance is deployed in fewer than two availability zones, which creates a single point of failure.",
		"zh": "ALB 实例部署在少于两个可用区，存在单点故障风险。",
		"ja": "ALB インスタンスが 2 つ未満の可用性ゾーンに展開されているため、単一障害点が発生します。",
		"de": "Die ALB-Instanz ist in weniger als zwei Verfügbarkeitszonen bereitgestellt, was einen Single Point of Failure schafft.",
		"es": "La instancia ALB está implementada en menos de dos zonas de disponibilidad, lo que crea un punto único de falla.",
		"fr": "L'instance ALB est déployée dans moins de deux zones de disponibilité, ce qui crée un point de défaillance unique.",
		"pt": "A instância ALB está implantada em menos de duas zonas de disponibilidade, criando um ponto único de falha.",
	},
	"recommendation": {
		"en": "Configure at least two zone mappings in ZoneMappings.",
		"zh": "在 ZoneMappings 中配置至少两个可用区映射。",
		"ja": "ZoneMappings に少なくとも 2 つのゾーンマッピングを設定します。",
		"de": "Konfigurieren Sie mindestens zwei Zonen-Zuordnungen in ZoneMappings.",
		"es": "Configure al menos dos mapeos de zona en ZoneMappings.",
		"fr": "Configurez au moins deux mappages de zone dans ZoneMappings.",
		"pt": "Configure pelo menos dois mapeamentos de zona em ZoneMappings.",
	},
	"resource_types": ["ALIYUN::ALB::LoadBalancer"],
}

is_multi_zone(resource) if {
	mappings := object.get(resource.Properties, "ZoneMappings", [])
	unique_zones := {zone |
		some mapping in mappings
		zone := object.get(mapping, "ZoneId", "")
		zone != ""
	}
	count(unique_zones) >= 2
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ALB::LoadBalancer")
	not is_multi_zone(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "ZoneMappings"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
