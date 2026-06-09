package infraguard.rules.aliyun.rds_instance_enabled_tde_disk_encryption

import rego.v1

import data.infraguard.helpers

# Rule metadata with i18n support
rule_meta := {
	"id": "rds-instance-enabled-tde-disk-encryption",
	"severity": "medium",
	"name": {
		"en": "RDS Instance Enabled TDE or Disk Encryption",
		"zh": "RDS 实例开启 TDE 或者数据盘加密",
		"ja": "RDS インスタンス TDE またはディスク暗号化が有効",
		"de": "RDS-Instanz TDE oder Festplattenverschlüsselung aktiviert",
		"es": "Instancia RDS TDE o Cifrado de Disco Habilitado",
		"fr": "Instance RDS TDE ou Chiffrement de Disque Activé",
		"pt": "Instância RDS TDE ou Criptografia de Disco Habilitada"
	},
	"description": {
		"en": "RDS instance should have TDE (Transparent Data Encryption) or disk encryption enabled.",
		"zh": "RDS 实例开启 TDE 或者数据盘加密，视为合规。",
		"ja": "RDS インスタンスで TDE（透過的データ暗号化）またはディスク暗号化を有効にする必要があります。",
		"de": "RDS-Instanz sollte TDE (Transparent Data Encryption) oder Festplattenverschlüsselung aktiviert haben.",
		"es": "La instancia RDS debe tener TDE (Cifrado Transparente de Datos) o cifrado de disco habilitado.",
		"fr": "L'instance RDS doit avoir TDE (Chiffrement Transparent des Données) ou le chiffrement de disque activé.",
		"pt": "A instância RDS deve ter TDE (Criptografia Transparente de Dados) ou criptografia de disco habilitada."
	},
	"reason": {
		"en": "RDS instance does not have TDE or disk encryption enabled, which may expose data to security risks.",
		"zh": "RDS 实例未开启 TDE 或数据盘加密，可能导致数据面临安全风险。",
		"ja": "RDS インスタンスで TDE またはディスク暗号化が有効になっていないため、データがセキュリティリスクにさらされる可能性があります。",
		"de": "RDS-Instanz hat keine TDE oder Festplattenverschlüsselung aktiviert, was Daten Sicherheitsrisiken aussetzen kann.",
		"es": "La instancia RDS no tiene TDE o cifrado de disco habilitado, lo que puede exponer los datos a riesgos de seguridad.",
		"fr": "L'instance RDS n'a pas TDE ou le chiffrement de disque activé, ce qui peut exposer les données à des risques de sécurité.",
		"pt": "A instância RDS não tem TDE ou criptografia de disco habilitada, o que pode expor os dados a riscos de segurança."
	},
	"recommendation": {
		"en": "Enable TDE by configuring EncryptionKey or use encrypted storage types (cloud_essd, cloud_essd2, cloud_essd3) for the RDS instance.",
		"zh": "通过配置 EncryptionKey 开启 TDE，或为 RDS 实例使用加密存储类型（cloud_essd、cloud_essd2、cloud_essd3）。",
		"ja": "EncryptionKey を設定して TDE を有効にするか、RDS インスタンスに暗号化ストレージタイプ（cloud_essd、cloud_essd2、cloud_essd3）を使用します。",
		"de": "Aktivieren Sie TDE, indem Sie EncryptionKey konfigurieren oder verwenden Sie verschlüsselte Speichertypen (cloud_essd, cloud_essd2, cloud_essd3) für die RDS-Instanz.",
		"es": "Habilite TDE configurando EncryptionKey o use tipos de almacenamiento cifrados (cloud_essd, cloud_essd2, cloud_essd3) para la instancia RDS.",
		"fr": "Activez TDE en configurant EncryptionKey ou utilisez des types de stockage chiffrés (cloud_essd, cloud_essd2, cloud_essd3) pour l'instance RDS.",
		"pt": "Habilite TDE configurando EncryptionKey ou use tipos de armazenamento criptografados (cloud_essd, cloud_essd2, cloud_essd3) para a instância RDS."
	},
	"resource_types": ["ALIYUN::RDS::DBInstance", "ALIYUN::RDS::PrepayDBInstance"]
}

# Encrypted storage types
encrypted_storage_types := ["cloud_essd", "cloud_essd2", "cloud_essd3"]

# Check if encryption is enabled (TDE via EncryptionKey or encrypted storage type)
is_encryption_enabled(resource) if {
	resource.Properties.EncryptionKey != null
}

is_encryption_enabled(resource) if {
	resource.Properties.DBInstanceStorageType in encrypted_storage_types
}

# Generate deny for non-compliant RDS instance resources
deny contains result if {
	some name, resource in helpers.resources_by_types(rule_meta.resource_types)
	not is_encryption_enabled(resource)
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
