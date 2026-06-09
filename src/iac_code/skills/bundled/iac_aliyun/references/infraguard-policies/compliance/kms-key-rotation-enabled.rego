package infraguard.rules.aliyun.kms_key_rotation_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "kms-key-rotation-enabled",
	"severity": "medium",
	"name": {
		"en": "KMS key automatic rotation enabled",
		"zh": "密钥管理服务设置主密钥自动轮转",
		"ja": "KMS キーの自動ローテーションが有効",
		"de": "KMS-Schlüssel automatische Rotation aktiviert",
		"es": "Rotación automática de clave KMS habilitada",
		"fr": "Rotation automatique de clé KMS activée",
		"pt": "Rotação automática de chave KMS habilitada"
	},
	"description": {
		"en": "KMS user master key has automatic rotation enabled, considered compliant. Service keys and externally imported keys are not applicable.",
		"zh": "对密钥管理服务中的用户主密钥设置自动轮转,视为合规。如果是服务密钥,视为不适用。如果来源是用户自带密钥,视为不适用。",
		"ja": "KMS ユーザーマスターキーで自動ローテーションが有効になっている場合、準拠と見なされます。サービスキーと外部からインポートされたキーは適用されません。",
		"de": "KMS-Benutzer-Hauptschlüssel hat automatische Rotation aktiviert, wird als konform betrachtet. Dienstschlüssel und extern importierte Schlüssel sind nicht anwendbar.",
		"es": "La clave maestra de usuario KMS tiene rotación automática habilitada, considerada conforme. Las claves de servicio y las claves importadas externamente no son aplicables.",
		"fr": "La clé maître utilisateur KMS a la rotation automatique activée, considérée comme conforme. Les clés de service et les clés importées externement ne sont pas applicables.",
		"pt": "Chave mestra de usuário KMS tem rotação automática habilitada, considerada conforme. Chaves de serviço e chaves importadas externamente não são aplicáveis."
	},
	"reason": {
		"en": "KMS key does not have automatic rotation enabled",
		"zh": "KMS 主密钥未开启自动轮转",
		"ja": "KMS キーで自動ローテーションが有効になっていません",
		"de": "KMS-Schlüssel hat keine automatische Rotation aktiviert",
		"es": "La clave KMS no tiene rotación automática habilitada",
		"fr": "La clé KMS n'a pas la rotation automatique activée",
		"pt": "Chave KMS não tem rotação automática habilitada"
	},
	"recommendation": {
		"en": "Enable automatic rotation for KMS key to enhance security by regularly rotating encryption keys",
		"zh": "为 KMS 主密钥启用自动轮转以通过定期轮换加密密钥来增强安全性",
		"ja": "暗号化キーを定期的にローテーションしてセキュリティを強化するために、KMS キーで自動ローテーションを有効にします",
		"de": "Aktivieren Sie die automatische Rotation für KMS-Schlüssel, um die Sicherheit durch regelmäßige Rotation von Verschlüsselungsschlüsseln zu verbessern",
		"es": "Habilite la rotación automática para la clave KMS para mejorar la seguridad rotando claves de cifrado regularmente",
		"fr": "Activez la rotation automatique pour la clé KMS pour améliorer la sécurité en faisant tourner régulièrement les clés de chiffrement",
		"pt": "Habilite rotação automática para chave KMS para melhorar a segurança rotacionando chaves de criptografia regularmente"
	},
	"resource_types": ["ALIYUN::KMS::Key"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::KMS::Key")

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
