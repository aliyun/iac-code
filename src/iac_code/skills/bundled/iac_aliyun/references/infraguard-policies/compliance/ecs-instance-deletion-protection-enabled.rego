package infraguard.rules.aliyun.ecs_instance_deletion_protection_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ecs-instance-deletion-protection-enabled",
	"severity": "high",
	"name": {
		"en": "ECS Instance Deletion Protection Enabled",
		"zh": "ECS 实例开启释放保护",
		"ja": "ECS インスタンスの削除保護が有効",
		"de": "ECS-Instanz Löschschutz aktiviert",
		"es": "Protección de Eliminación de Instancia ECS Habilitada",
		"fr": "Protection contre la Suppression d'Instance ECS Activée",
		"pt": "Proteção contra Exclusão de Instância ECS Habilitada"
	},
	"description": {
		"en": "Ensures that ECS instances have deletion protection enabled.",
		"zh": "确保 ECS 实例开启了释放保护。",
		"ja": "ECS インスタンスで削除保護が有効になっていることを確認します。",
		"de": "Stellt sicher, dass ECS-Instanzen Löschschutz aktiviert haben.",
		"es": "Asegura que las instancias ECS tengan habilitada la protección contra eliminación.",
		"fr": "Garantit que les instances ECS ont la protection contre la suppression activée.",
		"pt": "Garante que as instâncias ECS tenham proteção contra exclusão habilitada."
	},
	"reason": {
		"en": "If deletion protection is not enabled, the instance may be released accidentally, causing service interruption or data loss.",
		"zh": "如果未开启释放保护，实例可能会被意外释放，导致业务中断或数据丢失。",
		"ja": "削除保護が有効になっていない場合、インスタンスが誤って解放され、サービス中断やデータ損失が発生する可能性があります。",
		"de": "Wenn der Löschschutz nicht aktiviert ist, kann die Instanz versehentlich freigegeben werden, was zu Dienstunterbrechungen oder Datenverlust führt.",
		"es": "Si la protección contra eliminación no está habilitada, la instancia puede ser liberada accidentalmente, causando interrupción del servicio o pérdida de datos.",
		"fr": "Si la protection contre la suppression n'est pas activée, l'instance peut être libérée accidentellement, provoquant une interruption de service ou une perte de données.",
		"pt": "Se a proteção contra exclusão não estiver habilitada, a instância pode ser liberada acidentalmente, causando interrupção do serviço ou perda de dados."
	},
	"recommendation": {
		"en": "Enable deletion protection for the ECS instance.",
		"zh": "为 ECS 实例开启释放保护功能。",
		"ja": "ECS インスタンスで削除保護を有効にします。",
		"de": "Aktivieren Sie den Löschschutz für die ECS-Instanz.",
		"es": "Habilite la protección contra eliminación para la instancia ECS.",
		"fr": "Activez la protection contre la suppression pour l'instance ECS.",
		"pt": "Habilite proteção contra exclusão para a instância ECS."
	},
	"resource_types": ["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"]
}

is_compliant(resource) if {
	helpers.is_true(helpers.get_property(resource, "DeletionProtection", false))
}

deny contains result if {
	some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "DeletionProtection"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
