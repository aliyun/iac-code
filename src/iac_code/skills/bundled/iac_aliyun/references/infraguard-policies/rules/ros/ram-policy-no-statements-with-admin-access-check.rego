package infraguard.rules.aliyun.ram_policy_no_statements_with_admin_access_check

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "ram-policy-no-statements-with-admin-access-check",
	"severity": "high",
	"name": {
		"en": "RAM Policy No Admin Access",
		"zh": "禁止 RAM 策略包含管理员权限",
		"ja": "RAM ポリシーに管理者アクセスなし",
		"de": "RAM-Richtlinie kein Admin-Zugriff",
		"es": "Política RAM Sin Acceso de Administrador",
		"fr": "Politique RAM Sans Accès Administrateur",
		"pt": "Política RAM Sem Acesso de Administrador"
	},
	"description": {
		"en": "Ensures custom RAM policies do not grant full AdministratorAccess.",
		"zh": "确保自定义 RAM 策略未授予完全的管理员权限（AdministratorAccess）。",
		"ja": "カスタム RAM ポリシーが完全な AdministratorAccess を付与していないことを確認します。",
		"de": "Stellt sicher, dass benutzerdefinierte RAM-Richtlinien keinen vollständigen AdministratorAccess gewähren.",
		"es": "Garantiza que las políticas RAM personalizadas no otorguen acceso completo de administrador.",
		"fr": "Garantit que les politiques RAM personnalisées n'accordent pas un accès administrateur complet.",
		"pt": "Garante que políticas RAM personalizadas não concedam acesso completo de administrador."
	},
	"reason": {
		"en": "Granting excessive permissions increases the impact of a compromised account.",
		"zh": "授予过高权限会增加账号被盗后的危害。",
		"ja": "過剰な権限を付与すると、侵害されたアカウントの影響が増大します。",
		"de": "Das Gewähren übermäßiger Berechtigungen erhöht die Auswirkungen eines kompromittierten Kontos.",
		"es": "Otorgar permisos excesivos aumenta el impacto de una cuenta comprometida.",
		"fr": "Accorder des permissions excessives augmente l'impact d'un compte compromis.",
		"pt": "Conceder permissões excessivas aumenta o impacto de uma conta comprometida."
	},
	"recommendation": {
		"en": "Follow the principle of least privilege. Do not use '*' for both Action and Resource in the same statement.",
		"zh": "遵循最小权限原则。不要在同一条语句中对 Action 和 Resource 同时使用 '*'。",
		"ja": "最小権限の原則に従います。同じステートメントで Action と Resource の両方に '*' を使用しないでください。",
		"de": "Befolgen Sie das Prinzip der geringsten Berechtigung. Verwenden Sie nicht '*' für sowohl Action als auch Resource in derselben Anweisung.",
		"es": "Siga el principio de menor privilegio. No use '*' para tanto Action como Resource en la misma declaración.",
		"fr": "Suivez le principe du moindre privilège. N'utilisez pas '*' pour Action et Resource dans la même déclaration.",
		"pt": "Siga o princípio do menor privilégio. Não use '*' para Action e Resource na mesma declaração."
	},
	"resource_types": ["ALIYUN::RAM::ManagedPolicy"]
}

is_compliant(resource) if {
	doc := helpers.get_property(resource, "PolicyDocument", {})
	statements := object.get(doc, "Statement", [])
	not has_admin_statement(statements)
}

has_admin_statement(statements) if {
	some statement in statements
	statement.Effect == "Allow"
	is_all(statement.Action)
	is_all(statement.Resource)
}

is_all("*") := true

is_all(a) if {
	is_array(a)
	some item in a
	item == "*"
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RAM::ManagedPolicy")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "PolicyDocument"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
