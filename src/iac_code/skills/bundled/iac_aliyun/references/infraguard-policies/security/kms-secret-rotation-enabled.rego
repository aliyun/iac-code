package infraguard.rules.aliyun.kms_secret_rotation_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "kms-secret-rotation-enabled",
	"severity": "medium",
	"name": {
		"en": "KMS secret automatic rotation enabled",
		"zh": "密钥管理服务设置凭据自动轮转",
		"ja": "KMS シークレットの自動ローテーションが有効",
		"de": "KMS-Geheimnis automatische Rotation aktiviert",
		"es": "Rotación automática de secreto KMS habilitada",
		"fr": "Rotation automatique de secret KMS activée",
		"pt": "Rotação automática de segredo KMS habilitada"
	},
	"description": {
		"en": "KMS secret has automatic rotation enabled, considered compliant. Generic secrets are not applicable.",
		"zh": "密钥管理服务中的凭据设置自动轮转,视为合规。如果密钥类型为普通密钥,视为不适用。",
		"ja": "KMS シークレットで自動ローテーションが有効になっている場合、準拠と見なされます。汎用シークレットは適用されません。",
		"de": "KMS-Geheimnis hat automatische Rotation aktiviert, wird als konform betrachtet. Generische Geheimnisse sind nicht anwendbar.",
		"es": "El secreto KMS tiene rotación automática habilitada, considerado conforme. Los secretos genéricos no son aplicables.",
		"fr": "Le secret KMS a la rotation automatique activée, considéré comme conforme. Les secrets génériques ne sont pas applicables.",
		"pt": "Segredo KMS tem rotação automática habilitada, considerado conforme. Segredos genéricos não são aplicáveis."
	},
	"reason": {
		"en": "KMS secret does not have automatic rotation enabled",
		"zh": "KMS 凭据未开启自动轮转",
		"ja": "KMS シークレットで自動ローテーションが有効になっていません",
		"de": "KMS-Geheimnis hat keine automatische Rotation aktiviert",
		"es": "El secreto KMS no tiene rotación automática habilitada",
		"fr": "Le secret KMS n'a pas la rotation automatique activée",
		"pt": "Segredo KMS não tem rotação automática habilitada"
	},
	"recommendation": {
		"en": "Enable automatic rotation for KMS secret to enhance security by regularly rotating credentials",
		"zh": "为 KMS 凭据启用自动轮转以通过定期轮换凭据来增强安全性",
		"ja": "認証情報を定期的にローテーションしてセキュリティを強化するために、KMS シークレットで自動ローテーションを有効にします",
		"de": "Aktivieren Sie die automatische Rotation für KMS-Geheimnisse, um die Sicherheit durch regelmäßige Rotation von Anmeldeinformationen zu verbessern",
		"es": "Habilite la rotación automática para el secreto KMS para mejorar la seguridad rotando credenciales regularmente",
		"fr": "Activez la rotation automatique pour le secret KMS pour améliorer la sécurité en faisant tourner régulièrement les identifiants",
		"pt": "Habilite rotação automática para segredo KMS para melhorar a segurança rotacionando credenciais regularmente"
	},
	"resource_types": ["ALIYUN::KMS::Secret"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::KMS::Secret")

	# Check if EnableAutomaticRotation is enabled
	rotation_enabled := helpers.get_property(resource, "EnableAutomaticRotation", false)
	not helpers.is_true(rotation_enabled)

	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
