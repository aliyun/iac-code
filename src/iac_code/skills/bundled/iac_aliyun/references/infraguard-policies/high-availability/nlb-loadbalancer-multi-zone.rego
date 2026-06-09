package infraguard.rules.aliyun.nlb_loadbalancer_multi_zone

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "nlb-loadbalancer-multi-zone",
	"severity": "high",
	"name": {
		"en": "NLB Load Balancer Multi-Zone Deployment",
		"zh": "使用多可用区的 NLB 实例",
		"ja": "NLB ロードバランサーのマルチゾーン展開",
		"de": "NLB Load Balancer Multi-Zone-Bereitstellung",
		"es": "Implementación Multi-zona del Balanceador NLB",
		"fr": "Déploiement Multi-Zones de l'Équilibreur NLB",
		"pt": "Implantação Multi-zona do Balanceador NLB",
	},
	"description": {
		"en": "NLB instances should span at least two zones to support active-active traffic distribution and zone failover.",
		"zh": "NLB 实例应跨至少两个可用区，以支持多可用区主动主动流量分发和可用区故障转移。",
		"ja": "NLB インスタンスは、アクティブアクティブのトラフィック分散とゾーンフェイルオーバーをサポートするため、少なくとも 2 つのゾーンにまたがる必要があります。",
		"de": "NLB-Instanzen sollten mindestens zwei Zonen umfassen, um Active-Active-Traffic-Verteilung und Zonen-Failover zu unterstützen.",
		"es": "Las instancias NLB deben abarcar al menos dos zonas para admitir distribución de tráfico activo-activo y conmutación por error de zona.",
		"fr": "Les instances NLB doivent couvrir au moins deux zones pour prendre en charge la distribution de trafic actif-actif et le basculement de zone.",
		"pt": "Instâncias NLB devem abranger pelo menos duas zonas para oferecer distribuição de tráfego ativo-ativo e failover de zona.",
	},
	"reason": {
		"en": "The NLB instance is deployed in fewer than two availability zones, which weakens zone-level disaster recovery.",
		"zh": "NLB 实例部署在少于两个可用区，削弱了可用区级容灾能力。",
		"ja": "NLB インスタンスが 2 つ未満の可用性ゾーンに展開されているため、ゾーンレベルの災害復旧能力が低下します。",
		"de": "Die NLB-Instanz ist in weniger als zwei Verfügbarkeitszonen bereitgestellt, wodurch die zonenbezogene Disaster-Recovery-Fähigkeit geschwächt wird.",
		"es": "La instancia NLB está implementada en menos de dos zonas de disponibilidad, lo que debilita la recuperación ante desastres a nivel de zona.",
		"fr": "L'instance NLB est déployée dans moins de deux zones de disponibilité, ce qui affaiblit la reprise après sinistre au niveau de la zone.",
		"pt": "A instância NLB está implantada em menos de duas zonas de disponibilidade, enfraquecendo a recuperação de desastres no nível da zona.",
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
	"resource_types": ["ALIYUN::NLB::LoadBalancer"],
}

has_multiple_zones(resource) if {
	zone_mappings := object.get(resource.Properties, "ZoneMappings", [])
	count(zone_mappings) >= 2
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::NLB::LoadBalancer")
	not has_multiple_zones(resource)
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
