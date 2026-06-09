package infraguard.packs.aliyun.iac_code_best_practice

import rego.v1

pack_meta := {
	"id": "iac-code-best-practice",
	"name": {
		"en": "IaC Code Best Practice Pack",
		"zh": "IaC Code 最佳实践合规包",
		"ja": "IaC Code ベストプラクティスパック",
		"de": "IaC Code Best Practices Paket",
		"es": "Paquete de mejores prácticas de IaC Code",
		"fr": "Pack de meilleures pratiques IaC Code",
		"pt": "Pacote de melhores práticas do IaC Code"
	},
	"description": {
		"en": "Best practice checks for resource names, tags, and descriptions in Alibaba Cloud ROS templates.",
		"zh": "面向 Alibaba Cloud ROS 模板的资源名称、标签和描述最佳实践检查。",
		"ja": "Alibaba Cloud ROS テンプレートにおけるリソース名、タグ、説明のベストプラクティスチェック。",
		"de": "Best-Practice-Prüfungen für Ressourcennamen, Tags und Beschreibungen in Alibaba Cloud ROS-Vorlagen.",
		"es": "Comprobaciones de mejores prácticas para nombres, etiquetas y descripciones de recursos en plantillas ROS de Alibaba Cloud.",
		"fr": "Contrôles de meilleures pratiques pour les noms, étiquettes et descriptions des ressources dans les modèles ROS Alibaba Cloud.",
		"pt": "Verificações de melhores práticas para nomes, tags e descrições de recursos em templates ROS da Alibaba Cloud."
	},
	"rules": [
		"ecs-instance-tags-required",
		"ecs-instance-name-required",
		"ecs-security-group-description-required",
		"vpc-name-required",
		"vswitch-name-required",
		"rds-instance-tags-required",
		"redis-instance-name-required",
		"oss-bucket-tags-required",
		"slb-loadbalancer-name-required",
		"alb-loadbalancer-name-required",
		"polardb-cluster-tags-required",
		"sls-project-description-required",
		"kms-key-description-required"
	]
}
