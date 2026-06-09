package infraguard.packs.aliyun.cloud_infrastructure_security_baseline

import rego.v1

pack_meta := {
	"id": "cloud-infrastructure-security-baseline",
	"name": {
		"en": "Cloud Infrastructure Security Baseline",
		"zh": "云基础设施安全基线",
		"ja": "クラウドインフラストラクチャセキュリティベースライン",
		"de": "Cloud-Infrastruktur-Sicherheitsbaseline",
		"es": "Línea Base de Seguridad de Infraestructura en la Nube",
		"fr": "Référentiel de Sécurité de l'Infrastructure Cloud",
		"pt": "Linha de Base de Segurança da Infraestrutura em Nuvem"
	},
	"description": {
		"en": "Baseline policies for identity, network exposure, data protection, audit logging, supply chain, and key management in Alibaba Cloud ROS templates.",
		"zh": "面向阿里云 ROS 模板的身份、网络暴露、数据保护、审计日志、供应链和密钥管理安全基线。",
		"ja": "Alibaba Cloud ROS テンプレートにおける ID、ネットワーク公開、データ保護、監査ログ、サプライチェーン、鍵管理のベースラインポリシー。",
		"de": "Baseline-Richtlinien für Identität, Netzwerkexposition, Datenschutz, Audit-Protokollierung, Lieferkette und Schlüsselverwaltung in Alibaba Cloud ROS-Vorlagen.",
		"es": "Políticas base para identidad, exposición de red, protección de datos, registro de auditoría, cadena de suministro y gestión de claves en plantillas ROS de Alibaba Cloud.",
		"fr": "Politiques de base pour l'identité, l'exposition réseau, la protection des données, la journalisation d'audit, la chaîne d'approvisionnement et la gestion des clés dans les modèles ROS Alibaba Cloud.",
		"pt": "Políticas de linha de base para identidade, exposição de rede, proteção de dados, logs de auditoria, cadeia de suprimentos e gerenciamento de chaves em modelos ROS do Alibaba Cloud."
	},
	"rules": [
		"actiontrail-trail-intact-enabled",
		"vpc-flow-logs-enabled",
		"ram-user-mfa-check",
		"ram-password-policy-check",
		"ram-policy-no-statements-with-admin-access-check",
		"ecs-running-instance-no-public-ip",
		"ecs-security-group-risky-ports-check-with-protocol",
		"ecs-security-group-not-internet-cidr-access",
		"oss-bucket-server-side-encryption-enabled",
		"oss-bucket-only-https-enabled",
		"oss-bucket-public-read-prohibited",
		"oss-bucket-public-write-prohibited",
		"oss-bucket-logging-enabled",
		"rds-public-connection-and-any-ip-access-check",
		"rds-instance-enabled-ssl",
		"rds-instance-enabled-tde-disk-encryption",
		"redis-instance-no-public-ip",
		"redis-instance-enabled-ssl",
		"cr-repository-image-scanning-enabled",
		"cr-repository-type-private",
		"kms-key-rotation-enabled",
		"kms-secret-rotation-enabled",
		"fc-service-internet-access-disable",
		"api-gateway-api-auth-required",
		"api-gateway-api-internet-request-https"
	]
}
