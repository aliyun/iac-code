package infraguard.rules.aliyun.rds_instance_enabled_ssl

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "rds-instance-enabled-ssl",
	"severity": "medium",
	"name": {
		"en": "RDS Instance SSL Enabled",
		"zh": "RDS 实例开启 SSL 加密",
		"ja": "RDS インスタンスで SSL が有効",
		"de": "RDS-Instanz SSL aktiviert",
		"es": "SSL de Instancia RDS Habilitado",
		"fr": "SSL d'Instance RDS Activé",
		"pt": "SSL de Instância RDS Habilitado"
	},
	"description": {
		"en": "Ensures RDS instances have SSL encryption enabled.",
		"zh": "确保 RDS 实例开启了 SSL 加密。",
		"ja": "RDS インスタンスで SSL 暗号化が有効になっていることを確認します。",
		"de": "Stellt sicher, dass RDS-Instanzen SSL-Verschlüsselung aktiviert haben.",
		"es": "Garantiza que las instancias RDS tengan cifrado SSL habilitado.",
		"fr": "Garantit que les instances RDS ont le chiffrement SSL activé.",
		"pt": "Garante que as instâncias RDS tenham criptografia SSL habilitada."
	},
	"reason": {
		"en": "SSL encryption protects data in transit from eavesdropping and tampering.",
		"zh": "SSL 加密可保护传输中的数据免受窃听和篡改。",
		"ja": "SSL 暗号化により、送信中のデータが盗聴や改ざんから保護されます。",
		"de": "SSL-Verschlüsselung schützt Daten während der Übertragung vor Abhören und Manipulation.",
		"es": "El cifrado SSL protege los datos en tránsito contra interceptación y manipulación.",
		"fr": "Le chiffrement SSL protège les données en transit contre l'écoute et la falsification.",
		"pt": "A criptografia SSL protege dados em trânsito contra interceptação e adulteração."
	},
	"recommendation": {
		"en": "Enable SSL for the RDS instance.",
		"zh": "为 RDS 实例开启 SSL 加密。",
		"ja": "RDS インスタンスで SSL を有効にします。",
		"de": "Aktivieren Sie SSL für die RDS-Instanz.",
		"es": "Habilite SSL para la instancia RDS.",
		"fr": "Activez SSL pour l'instance RDS.",
		"pt": "Habilite SSL para a instância RDS."
	},
	"resource_types": ["ALIYUN::RDS::DBInstance"]
}

is_compliant(resource) if {
	ssl := helpers.get_property(resource, "SSLSetting", "Disabled")
	ssl != "Disabled"
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SSLSetting"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
