package infraguard.rules.aliyun.maxcompute_project_encryption_enabled

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "maxcompute-project-encryption-enabled",
	"severity": "high",
	"name": {
		"en": "MaxCompute Project Encryption Enabled",
		"zh": "MaxCompute 项目开启加密",
		"ja": "MaxCompute プロジェクト暗号化が有効",
		"de": "MaxCompute-Projekt Verschlüsselung aktiviert",
		"es": "Cifrado de Proyecto MaxCompute Habilitado",
		"fr": "Chiffrement de Projet MaxCompute Activé",
		"pt": "Criptografia de Projeto MaxCompute Habilitada"
	},
	"description": {
		"en": "Ensures MaxCompute projects have encryption enabled to protect stored data.",
		"zh": "确保 MaxCompute 项目启用了加密以保护存储的数据。",
		"ja": "MaxCompute プロジェクトで保存データを保護するために暗号化が有効になっていることを確認します。",
		"de": "Stellt sicher, dass MaxCompute-Projekte Verschlüsselung aktiviert haben, um gespeicherte Daten zu schützen.",
		"es": "Garantiza que los proyectos MaxCompute tengan cifrado habilitado para proteger los datos almacenados.",
		"fr": "Garantit que les projets MaxCompute ont le chiffrement activé pour protéger les données stockées.",
		"pt": "Garante que os projetos MaxCompute tenham criptografia habilitada para proteger dados armazenados."
	},
	"reason": {
		"en": "Encryption protects sensitive data stored in MaxCompute projects from unauthorized access.",
		"zh": "加密可以保护 MaxCompute 项目中存储的敏感数据免受非授权访问。",
		"ja": "暗号化により、MaxCompute プロジェクトに保存されている機密データが不正アクセスから保護されます。",
		"de": "Verschlüsselung schützt sensible Daten, die in MaxCompute-Projekten gespeichert sind, vor unbefugtem Zugriff.",
		"es": "El cifrado protege los datos sensibles almacenados en proyectos MaxCompute del acceso no autorizado.",
		"fr": "Le chiffrement protège les données sensibles stockées dans les projets MaxCompute contre l'accès non autorisé.",
		"pt": "A criptografia protege dados sensíveis armazenados em projetos MaxCompute contra acesso não autorizado."
	},
	"recommendation": {
		"en": "Enable encryption for the MaxCompute project.",
		"zh": "为 MaxCompute 项目启用加密。",
		"ja": "MaxCompute プロジェクトの暗号化を有効にします。",
		"de": "Aktivieren Sie die Verschlüsselung für das MaxCompute-Projekt.",
		"es": "Habilite el cifrado para el proyecto MaxCompute.",
		"fr": "Activez le chiffrement pour le projet MaxCompute.",
		"pt": "Habilite a criptografia para o projeto MaxCompute."
	},
	"resource_types": ["ALIYUN::MaxCompute::Project"]
}

# Check if encryption is enabled
is_compliant(resource) if {
	# Direct access to nested properties
	props := resource.Properties.Properties
	props != null
	encryption := props.Encryption
	encryption != null
	encryption.Enable == true
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::MaxCompute::Project")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "Encryption", "Enable"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
