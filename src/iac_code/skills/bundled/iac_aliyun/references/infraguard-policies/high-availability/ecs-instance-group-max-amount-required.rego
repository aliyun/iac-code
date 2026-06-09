package infraguard.rules.aliyun.ecs_instance_group_max_amount_required

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ecs-instance-group-max-amount-required",
	"severity": "medium",
	"name": {
		"en": "ECS Instance Group Maximum Amount Required",
		"zh": "ECS 实例组必须配置最大实例数",
		"ja": "ECS インスタンスグループの最大数必須",
		"de": "Maximalanzahl für ECS-Instanzgruppe erforderlich",
		"es": "Cantidad máxima requerida para grupo de instancias ECS",
		"fr": "Nombre maximal requis pour le groupe d'instances ECS",
		"pt": "Quantidade máxima obrigatória para grupo de instâncias ECS",
	},
	"description": {
		"en": "ECS instance groups should declare MaxAmount so the intended replica ceiling is explicit.",
		"zh": "ECS 实例组应声明 MaxAmount，使计划的副本上限清晰可见。",
		"ja": "ECS インスタンスグループは、意図したレプリカ上限を明確にするため MaxAmount を宣言する必要があります。",
		"de": "ECS-Instanzgruppen sollten MaxAmount deklarieren, damit die beabsichtigte Replikatobergrenze explizit ist.",
		"es": "Los grupos de instancias ECS deben declarar MaxAmount para que el límite de réplicas previsto sea explícito.",
		"fr": "Les groupes d'instances ECS doivent déclarer MaxAmount afin que le plafond de réplicas prévu soit explicite.",
		"pt": "Grupos de instâncias ECS devem declarar MaxAmount para tornar explícito o limite planejado de réplicas.",
	},
	"reason": {
		"en": "The ECS instance group does not specify MaxAmount.",
		"zh": "ECS 实例组未指定 MaxAmount。",
		"ja": "ECS インスタンスグループで MaxAmount が指定されていません。",
		"de": "Die ECS-Instanzgruppe gibt MaxAmount nicht an.",
		"es": "El grupo de instancias ECS no especifica MaxAmount.",
		"fr": "Le groupe d'instances ECS ne spécifie pas MaxAmount.",
		"pt": "O grupo de instâncias ECS não especifica MaxAmount.",
	},
	"recommendation": {
		"en": "Configure MaxAmount, preferably at least 2 for production workloads.",
		"zh": "配置 MaxAmount，生产工作负载建议至少为 2。",
		"ja": "MaxAmount を設定します。本番ワークロードでは少なくとも 2 を推奨します。",
		"de": "Konfigurieren Sie MaxAmount, für Produktionsworkloads vorzugsweise mindestens 2.",
		"es": "Configure MaxAmount, preferiblemente al menos 2 para cargas de trabajo de producción.",
		"fr": "Configurez MaxAmount, de préférence au moins 2 pour les charges de travail de production.",
		"pt": "Configure MaxAmount, de preferência pelo menos 2 para cargas de trabalho de produção.",
	},
	"resource_types": ["ALIYUN::ECS::InstanceGroup"],
}

has_max_amount(resource) if {
	helpers.has_property(resource, "MaxAmount")
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::InstanceGroup")
	not has_max_amount(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "MaxAmount"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
