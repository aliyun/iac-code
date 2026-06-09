package infraguard.rules.aliyun.cr_repository_type_private

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "cr-repository-type-private",
	"severity": "high",
	"name": {
		"en": "CR Repository Type Private",
		"zh": "容器镜像服务镜像仓库类型为私有",
		"ja": "CR リポジトリタイプがプライベート",
		"de": "CR-Repository-Typ privat",
		"es": "Tipo de Repositorio CR Privado",
		"fr": "Type de Dépôt CR Privé",
		"pt": "Tipo de Repositório CR Privado"
	},
	"description": {
		"en": "Ensures that CR repositories are set to PRIVATE.",
		"zh": "确保容器镜像仓库类型设置为私有。",
		"ja": "CR リポジトリが PRIVATE に設定されていることを確認します。",
		"de": "Stellt sicher, dass CR-Repositories auf PRIVATE gesetzt sind.",
		"es": "Garantiza que los repositorios CR estén configurados como PRIVADOS.",
		"fr": "Garantit que les dépôts CR sont définis sur PRIVÉ.",
		"pt": "Garante que os repositórios CR estejam definidos como PRIVADO."
	},
	"reason": {
		"en": "Public repositories can be accessed by anyone, which may lead to exposure of sensitive code or data.",
		"zh": "公开仓库可以被任何人访问，可能导致敏感代码或数据泄露。",
		"ja": "パブリックリポジトリは誰でもアクセスでき、機密コードやデータの露出につながる可能性があります。",
		"de": "Öffentliche Repositories können von jedem abgerufen werden, was zur Offenlegung sensibler Codes oder Daten führen kann.",
		"es": "Los repositorios públicos pueden ser accedidos por cualquiera, lo que puede llevar a la exposición de código o datos sensibles.",
		"fr": "Les dépôts publics peuvent être accessibles par n'importe qui, ce qui peut entraîner l'exposition de code ou de données sensibles.",
		"pt": "Repositórios públicos podem ser acessados por qualquer pessoa, o que pode levar à exposição de código ou dados sensíveis."
	},
	"recommendation": {
		"en": "Set the RepoType to 'PRIVATE' for the CR repository.",
		"zh": "将容器镜像仓库的 RepoType 设置为 'PRIVATE'。",
		"ja": "CR リポジトリの RepoType を 'PRIVATE' に設定します。",
		"de": "Setzen Sie den RepoType für das CR-Repository auf 'PRIVATE'.",
		"es": "Establezca el RepoType en 'PRIVADO' para el repositorio CR.",
		"fr": "Définissez le RepoType sur 'PRIVÉ' pour le dépôt CR.",
		"pt": "Defina o RepoType como 'PRIVADO' para o repositório CR."
	},
	"resource_types": ["ALIYUN::CR::Repository"]
}

is_compliant(resource) if {
	helpers.get_property(resource, "RepoType", "PRIVATE") == "PRIVATE"
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::CR::Repository")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "RepoType"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
