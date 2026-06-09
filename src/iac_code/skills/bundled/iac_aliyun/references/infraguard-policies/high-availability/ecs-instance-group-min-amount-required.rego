package infraguard.rules.aliyun.ecs_instance_group_min_amount_required

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ecs-instance-group-min-amount-required",
	"severity": "medium",
	"name": {
		"en": "ECS Instance Group Minimum Amount Required",
		"zh": "ECS 实例组必须配置最小实例数",
		"ja": "ECS インスタンスグループの最小数必須",
		"de": "Mindestanzahl für ECS-Instanzgruppe erforderlich",
		"es": "Cantidad mínima requerida para grupo de instancias ECS",
		"fr": "Nombre minimal requis pour le groupe d'instances ECS",
		"pt": "Quantidade mínima obrigatória para grupo de instâncias ECS",
	},
	"description": {
		"en": "ECS instance groups should declare MinAmount so the baseline replica count is explicit.",
		"zh": "ECS 实例组应声明 MinAmount，使基线副本数清晰可见。",
		"ja": "ECS インスタンスグループは、基準レプリカ数を明確にするため MinAmount を宣言する必要があります。",
		"de": "ECS-Instanzgruppen sollten MinAmount deklarieren, damit die Basis-Replikatanzahl explizit ist.",
		"es": "Los grupos de instancias ECS deben declarar MinAmount para que el número base de réplicas sea explícito.",
		"fr": "Les groupes d'instances ECS doivent déclarer MinAmount afin que le nombre de réplicas de base soit explicite.",
		"pt": "Grupos de instâncias ECS devem declarar MinAmount para tornar explícita a contagem base de réplicas.",
	},
	"reason": {
		"en": "The ECS instance group does not specify MinAmount.",
		"zh": "ECS 实例组未指定 MinAmount。",
		"ja": "ECS インスタンスグループで MinAmount が指定されていません。",
		"de": "Die ECS-Instanzgruppe gibt MinAmount nicht an.",
		"es": "El grupo de instancias ECS no especifica MinAmount.",
		"fr": "Le groupe d'instances ECS ne spécifie pas MinAmount.",
		"pt": "O grupo de instâncias ECS não especifica MinAmount.",
	},
	"recommendation": {
		"en": "Configure MinAmount, preferably at least 2 for production workloads.",
		"zh": "配置 MinAmount，生产工作负载建议至少为 2。",
		"ja": "MinAmount を設定します。本番ワークロードでは少なくとも 2 を推奨します。",
		"de": "Konfigurieren Sie MinAmount, für Produktionsworkloads vorzugsweise mindestens 2.",
		"es": "Configure MinAmount, preferiblemente al menos 2 para cargas de trabajo de producción.",
		"fr": "Configurez MinAmount, de préférence au moins 2 pour les charges de travail de production.",
		"pt": "Configure MinAmount, de preferência pelo menos 2 para cargas de trabalho de produção.",
	},
	"resource_types": ["ALIYUN::ECS::InstanceGroup"],
}

has_min_amount(resource) if {
	helpers.has_property(resource, "MinAmount")
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::InstanceGroup")
	not has_min_amount(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "MinAmount"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
