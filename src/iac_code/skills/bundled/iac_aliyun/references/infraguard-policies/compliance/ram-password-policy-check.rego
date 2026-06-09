package infraguard.rules.aliyun.ram_password_policy_check

import rego.v1

import data.infraguard.helpers

# Rule metadata
rule_meta := {
	"id": "ram-password-policy-check",
	"severity": "medium",
	"name": {
		"en": "RAM Password Policy Check",
		"zh": "RAM 密码策略检测",
		"ja": "RAM パスワードポリシーチェック",
		"de": "RAM-Passwortrichtlinien-Prüfung",
		"es": "Verificación de Política de Contraseña RAM",
		"fr": "Vérification de la Politique de Mot de Passe RAM",
		"pt": "Verificação de Política de Senha RAM"
	},
	"description": {
		"en": "Ensures that the RAM password policy meets the specified security requirements.",
		"zh": "确保 RAM 密码策略符合指定的安全要求。",
		"ja": "RAM パスワードポリシーが指定されたセキュリティ要件を満たしていることを確認します。",
		"de": "Stellt sicher, dass die RAM-Passwortrichtlinie die festgelegten Sicherheitsanforderungen erfüllt.",
		"es": "Garantiza que la política de contraseña RAM cumpla con los requisitos de seguridad especificados.",
		"fr": "Garantit que la politique de mot de passe RAM répond aux exigences de sécurité spécifiées.",
		"pt": "Garante que a política de senha RAM atenda aos requisitos de segurança especificados."
	},
	"reason": {
		"en": "Strong password policies help prevent unauthorized access to accounts.",
		"zh": "强密码策略有助于防止对账号的未经授权访问。",
		"ja": "強力なパスワードポリシーは、アカウントへの不正アクセスを防ぐのに役立ちます。",
		"de": "Starke Passwortrichtlinien helfen, unbefugten Zugriff auf Konten zu verhindern.",
		"es": "Las políticas de contraseña fuertes ayudan a prevenir el acceso no autorizado a las cuentas.",
		"fr": "Des politiques de mot de passe fortes aident à prévenir l'accès non autorisé aux comptes.",
		"pt": "Políticas de senha fortes ajudam a prevenir acesso não autorizado a contas."
	},
	"recommendation": {
		"en": "Configure a strong RAM password policy including length, character types, and rotation.",
		"zh": "配置强 RAM 密码策略，包括长度、字符类型和定期轮换。",
		"ja": "長さ、文字タイプ、ローテーションを含む強力な RAM パスワードポリシーを設定します。",
		"de": "Konfigurieren Sie eine starke RAM-Passwortrichtlinie, einschließlich Länge, Zeichentypen und Rotation.",
		"es": "Configure una política de contraseña RAM fuerte que incluya longitud, tipos de caracteres y rotación.",
		"fr": "Configurez une politique de mot de passe RAM forte incluant la longueur, les types de caractères et la rotation.",
		"pt": "Configure uma política de senha RAM forte incluindo comprimento, tipos de caracteres e rotação."
	},
	"resource_types": ["ALIYUN::RAM::PasswordPolicy"]
}

# This rule typically checks RAM::PasswordPolicy resources
# Since a ROS template might not have this, we check it if it exists.
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RAM::PasswordPolicy")

	# Logic to check properties like MinimumPasswordLength
	props := resource.Properties
	not props.MinimumPasswordLength >= 8
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "MinimumPasswordLength"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
