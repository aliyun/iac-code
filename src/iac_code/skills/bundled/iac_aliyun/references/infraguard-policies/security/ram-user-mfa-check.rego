package infraguard.rules.aliyun.ram_user_mfa_check

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "ram-user-mfa-check",
	"severity": "high",
	"name": {
		"en": "RAM User MFA Enabled",
		"zh": "RAM 用户开启 MFA",
		"ja": "RAM ユーザーで MFA が有効",
		"de": "RAM-Benutzer MFA aktiviert",
		"es": "MFA de Usuario RAM Habilitado",
		"fr": "MFA Utilisateur RAM Activé",
		"pt": "MFA de Usuário RAM Habilitado"
	},
	"description": {
		"en": "RAM users with console access should have multi-factor authentication (MFA) enabled.",
		"zh": "检测 RAM 用户是否开通 MFA 二次验证登录，开通视为合规。",
		"ja": "コンソールアクセスを持つ RAM ユーザーは、多要素認証（MFA）を有効にする必要があります。",
		"de": "RAM-Benutzer mit Konsolenzugriff sollten Multi-Faktor-Authentifizierung (MFA) aktiviert haben.",
		"es": "Los usuarios RAM con acceso a la consola deben tener habilitada la autenticación multifactor (MFA).",
		"fr": "Les utilisateurs RAM avec accès à la console doivent avoir l'authentification multifacteur (MFA) activée.",
		"pt": "Usuários RAM com acesso ao console devem ter autenticação multifator (MFA) habilitada."
	},
	"reason": {
		"en": "RAM users without MFA are vulnerable to password compromise, posing a significant security risk.",
		"zh": "RAM 用户未开启 MFA，一旦密码泄露，账号将面临极大的安全风险。",
		"ja": "MFA がない RAM ユーザーはパスワードの侵害に対して脆弱で、重大なセキュリティリスクをもたらします。",
		"de": "RAM-Benutzer ohne MFA sind anfällig für Passwortkompromittierung und stellen ein erhebliches Sicherheitsrisiko dar.",
		"es": "Los usuarios RAM sin MFA son vulnerables al compromiso de contraseñas, lo que plantea un riesgo de seguridad significativo.",
		"fr": "Les utilisateurs RAM sans MFA sont vulnérables au compromis de mot de passe, ce qui pose un risque de sécurité important.",
		"pt": "Usuários RAM sem MFA são vulneráveis a comprometimento de senha, representando um risco significativo de segurança."
	},
	"recommendation": {
		"en": "Enable MFA for the RAM user by setting LoginProfile.MFABindRequired to true.",
		"zh": "通过将 LoginProfile.MFABindRequired 设置为 true 为 RAM 用户强制开启 MFA。",
		"ja": "LoginProfile.MFABindRequired を true に設定して、RAM ユーザーで MFA を有効にします。",
		"de": "Aktivieren Sie MFA für den RAM-Benutzer, indem Sie LoginProfile.MFABindRequired auf true setzen.",
		"es": "Habilite MFA para el usuario RAM estableciendo LoginProfile.MFABindRequired en true.",
		"fr": "Activez MFA pour l'utilisateur RAM en définissant LoginProfile.MFABindRequired sur true.",
		"pt": "Habilite MFA para o usuário RAM definindo LoginProfile.MFABindRequired como true."
	},
	"resource_types": ["ALIYUN::RAM::User"]
}

# Check if MFA is required for login
is_mfa_enabled(resource) if {
	login_profile := helpers.get_property(resource, "LoginProfile", {})
	mfa := object.get(login_profile, "MFABindRequired", false)
	helpers.is_true(mfa)
}

deny contains result if {
	some name, resource in helpers.resources_by_types(rule_meta.resource_types)

	# Only check users who have console access (LoginProfile exists)
	helpers.has_property(resource, "LoginProfile")
	not is_mfa_enabled(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "LoginProfile", "MFABindRequired"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
