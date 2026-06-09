package infraguard.rules.aliyun.nas_filesystem_encrypt_type_check

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "nas-filesystem-encrypt-type-check",
	"severity": "low",
	"name": {
		"en": "NAS file system encryption configured",
		"zh": "NAS 文件系统设置了加密",
		"ja": "NAS ファイルシステムの暗号化が設定されている",
		"de": "NAS-Dateisystem-Verschlüsselung konfiguriert",
		"es": "Cifrado de Sistema de Archivos NAS Configurado",
		"fr": "Chiffrement du Système de Fichiers NAS Configuré",
		"pt": "Criptografia do Sistema de Arquivos NAS Configurada"
	},
	"description": {
		"en": "NAS file system has encryption configured, considered compliant.",
		"zh": "NAS 文件系统设置了加密,视为合规。",
		"ja": "NAS ファイルシステムに暗号化が設定されており、準拠と見なされます。",
		"de": "NAS-Dateisystem hat Verschlüsselung konfiguriert, gilt als konform.",
		"es": "El sistema de archivos NAS tiene cifrado configurado, se considera conforme.",
		"fr": "Le système de fichiers NAS a le chiffrement configuré, considéré comme conforme.",
		"pt": "O sistema de arquivos NAS tem criptografia configurada, considerado em conformidade."
	},
	"reason": {
		"en": "NAS file system does not have encryption configured",
		"zh": "NAS 文件系统未设置加密",
		"ja": "NAS ファイルシステムに暗号化が設定されていません",
		"de": "NAS-Dateisystem hat keine Verschlüsselung konfiguriert",
		"es": "El sistema de archivos NAS no tiene cifrado configurado",
		"fr": "Le système de fichiers NAS n'a pas de chiffrement configuré",
		"pt": "O sistema de arquivos NAS não tem criptografia configurada"
	},
	"recommendation": {
		"en": "Configure encryption for NAS file system to protect data at rest using KMS keys",
		"zh": "为 NAS 文件系统配置加密以使用 KMS 密钥保护静态数据",
		"ja": "KMS キーを使用して保存データを保護するために、NAS ファイルシステムの暗号化を設定します",
		"de": "Konfigurieren Sie Verschlüsselung für das NAS-Dateisystem, um Daten im Ruhezustand mit KMS-Schlüsseln zu schützen",
		"es": "Configure el cifrado para el sistema de archivos NAS para proteger los datos en reposo usando claves KMS",
		"fr": "Configurez le chiffrement pour le système de fichiers NAS pour protéger les données au repos à l'aide de clés KMS",
		"pt": "Configure a criptografia para o sistema de arquivos NAS para proteger dados em repouso usando chaves KMS"
	},
	"resource_types": ["ALIYUN::NAS::FileSystem"]
}

is_encrypted(resource) if {
	encrypt_type := helpers.get_property(resource, "EncryptType", 0)
	encrypt_type == 1
}

is_encrypted(resource) if {
	encrypt_type := helpers.get_property(resource, "EncryptType", 0)
	encrypt_type == 2
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::NAS::FileSystem")
	not is_encrypted(resource)
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
