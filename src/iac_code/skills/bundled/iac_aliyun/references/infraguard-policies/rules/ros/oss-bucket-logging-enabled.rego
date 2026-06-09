package infraguard.rules.aliyun.oss_bucket_logging_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "oss-bucket-logging-enabled",
	"severity": "medium",
	"name": {
		"en": "OSS Bucket Logging Enabled",
		"zh": "OSS 存储空间开启日志转存",
		"ja": "OSS バケットのログ記録が有効",
		"de": "OSS-Bucket Protokollierung aktiviert",
		"es": "Registro de Logs de Bucket OSS Habilitado",
		"fr": "Journalisation de Bucket OSS Activée",
		"pt": "Registro de Logs de Bucket OSS Habilitado"
	},
	"description": {
		"en": "OSS buckets should have logging enabled to track access and operations. Logging helps with security auditing, troubleshooting, and compliance requirements.",
		"zh": "OSS 存储空间应开启日志转存以跟踪访问和操作。日志记录有助于安全审计、故障排查和合规要求。",
		"ja": "OSS バケットは、アクセスと操作を追跡するためにログ記録を有効にする必要があります。ログ記録は、セキュリティ監査、トラブルシューティング、コンプライアンス要件に役立ちます。",
		"de": "OSS-Buckets sollten Protokollierung aktiviert haben, um Zugriffe und Vorgänge zu verfolgen. Die Protokollierung hilft bei Sicherheitsaudits, Fehlerbehebung und Compliance-Anforderungen.",
		"es": "Los buckets OSS deben tener registro de logs habilitado para rastrear acceso y operaciones. El registro de logs ayuda en la auditoría de seguridad, solución de problemas y requisitos de conformidad.",
		"fr": "Les buckets OSS doivent avoir la journalisation activée pour suivre les accès et les opérations. La journalisation aide à l'audit de sécurité, au dépannage et aux exigences de conformité.",
		"pt": "Buckets OSS devem ter registro de logs habilitado para rastrear acesso e operações. O registro de logs ajuda na auditoria de segurança, solução de problemas e requisitos de conformidade."
	},
	"reason": {
		"en": "The OSS bucket does not have logging enabled, which makes it difficult to track access and operations for security and compliance purposes.",
		"zh": "OSS 存储空间未开启日志转存，难以跟踪访问和操作以满足安全和合规要求。",
		"ja": "OSS バケットでログ記録が有効になっていないため、セキュリティとコンプライアンスの目的でアクセスと操作を追跡することが困難です。",
		"de": "Der OSS-Bucket hat keine Protokollierung aktiviert, was es schwierig macht, Zugriffe und Vorgänge für Sicherheits- und Compliance-Zwecke zu verfolgen.",
		"es": "El bucket OSS no tiene registro de logs habilitado, lo que dificulta rastrear acceso y operaciones para fines de seguridad y conformidad.",
		"fr": "Le bucket OSS n'a pas la journalisation activée, ce qui rend difficile le suivi des accès et des opérations à des fins de sécurité et de conformité.",
		"pt": "O bucket OSS não tem registro de logs habilitado, o que dificulta rastrear acesso e operações para fins de segurança e conformidade."
	},
	"recommendation": {
		"en": "Enable logging for the OSS bucket by configuring the LoggingConfiguration property with TargetBucket and optionally TargetPrefix.",
		"zh": "通过配置 LoggingConfiguration 属性并设置 TargetBucket 和可选的 TargetPrefix，为 OSS 存储空间启用日志转存。",
		"ja": "TargetBucket とオプションで TargetPrefix を使用して LoggingConfiguration プロパティを設定することで、OSS バケットのログ記録を有効にします。",
		"de": "Aktivieren Sie die Protokollierung für den OSS-Bucket, indem Sie die LoggingConfiguration-Eigenschaft mit TargetBucket und optional TargetPrefix konfigurieren.",
		"es": "Habilite el registro de logs para el bucket OSS configurando la propiedad LoggingConfiguration con TargetBucket y opcionalmente TargetPrefix.",
		"fr": "Activez la journalisation pour le bucket OSS en configurant la propriété LoggingConfiguration avec TargetBucket et éventuellement TargetPrefix.",
		"pt": "Habilite registro de logs para o bucket OSS configurando a propriedade LoggingConfiguration com TargetBucket e opcionalmente TargetPrefix."
	},
	"resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
	not has_logging_enabled(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "LoggingConfiguration"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}

has_logging_enabled(resource) if {
	helpers.has_property(resource, "LoggingConfiguration")
	logging_config := resource.Properties.LoggingConfiguration
	logging_config.TargetBucket != null
}
