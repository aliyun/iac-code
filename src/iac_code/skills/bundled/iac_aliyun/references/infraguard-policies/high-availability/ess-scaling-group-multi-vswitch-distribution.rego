package infraguard.rules.aliyun.ess_scaling_group_multi_vswitch_distribution

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ess-scaling-group-multi-vswitch-distribution",
	"severity": "high",
	"name": {
		"en": "ESS Scaling Group Multi-VSwitch Distribution",
		"zh": "弹性伸缩组关联至少两个交换机",
		"ja": "ESS スケーリンググループマルチ VSwitch",
		"de": "ESS-Skalierungsgruppe Multi-VSwitch",
		"es": "Grupo de escalado ESS con múltiples VSwitches",
		"fr": "Groupe ESS avec plusieurs VSwitches",
		"pt": "Grupo ESS com múltiplos VSwitches",
	},
	"description": {
		"en": "ESS scaling groups should attach at least two VSwitches so instances can be distributed across zones for high availability.",
		"zh": "弹性伸缩组应关联至少两个交换机，使实例可以跨可用区分布。",
		"ja": "ESS スケーリンググループは、インスタンスをゾーン間で分散できるように少なくとも 2 つの VSwitch を関連付ける必要があります。",
		"de": "ESS-Skalierungsgruppen sollten mindestens zwei VSwitches zuordnen, damit Instanzen über Zonen verteilt werden können.",
		"es": "Los grupos de escalado ESS deben asociar al menos dos VSwitches para distribuir instancias entre zonas.",
		"fr": "Les groupes ESS doivent associer au moins deux VSwitches afin de répartir les instances entre les zones.",
		"pt": "Grupos ESS devem associar pelo menos dois VSwitches para distribuir instâncias entre zonas.",
	},
	"reason": {
		"en": "The ESS scaling group has fewer than two VSwitchIds.",
		"zh": "弹性伸缩组的 VSwitchIds 少于两个。",
		"ja": "ESS スケーリンググループの VSwitchIds が 2 つ未満です。",
		"de": "Die ESS-Skalierungsgruppe hat weniger als zwei VSwitchIds.",
		"es": "El grupo de escalado ESS tiene menos de dos VSwitchIds.",
		"fr": "Le groupe ESS a moins de deux VSwitchIds.",
		"pt": "O grupo ESS tem menos de dois VSwitchIds.",
	},
	"recommendation": {
		"en": "Configure at least two VSwitchIds.",
		"zh": "配置至少两个 VSwitchIds。",
		"ja": "少なくとも 2 つの VSwitchIds を設定します。",
		"de": "Konfigurieren Sie mindestens zwei VSwitchIds.",
		"es": "Configure al menos dos VSwitchIds.",
		"fr": "Configurez au moins deux VSwitchIds.",
		"pt": "Configure pelo menos dois VSwitchIds.",
	},
	"resource_types": ["ALIYUN::ESS::ScalingGroup"],
}

has_multiple_vswitches(resource) if {
	vswitches := object.get(resource.Properties, "VSwitchIds", [])
	count(vswitches) >= 2
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
	not has_multiple_vswitches(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "VSwitchIds"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
