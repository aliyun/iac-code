package infraguard.rules.aliyun.oss_bucket_server_side_encryption_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "oss-bucket-server-side-encryption-enabled",
	"severity": "high",
	"name": {
		"en": "OSS Bucket Server-Side Encryption Enabled",
		"zh": "OSS 存储空间开启服务端加密",
		"ja": "OSS バケットのサーバー側暗号化が有効",
		"de": "OSS-Bucket-Serverseitige-Verschlüsselung aktiviert",
		"es": "Cifrado del Lado del Servidor de Bucket OSS Habilitado",
		"fr": "Chiffrement Côté Serveur de Bucket OSS Activé",
		"pt": "Criptografia do Lado do Servidor de Bucket OSS Habilitada"
	},
	"description": {
		"en": "OSS buckets should have server-side encryption enabled to protect data at rest. Server-side encryption uses KMS or AES256 to encrypt data stored in OSS.",
		"zh": "OSS 存储空间应开启服务端加密以保护静态数据。服务端加密使用 KMS 或 AES256 对存储在 OSS 中的数据进行加密。",
		"ja": "OSS バケットは、保存データを保護するためにサーバー側暗号化を有効にする必要があります。サーバー側暗号化は KMS または AES256 を使用して OSS に保存されたデータを暗号化します。",
		"de": "OSS-Buckets sollten serverseitige Verschlüsselung aktiviert haben, um ruhende Daten zu schützen. Die serverseitige Verschlüsselung verwendet KMS oder AES256, um in OSS gespeicherte Daten zu verschlüsseln.",
		"es": "Los buckets OSS deben tener cifrado del lado del servidor habilitado para proteger los datos en reposo. El cifrado del lado del servidor usa KMS o AES256 para cifrar datos almacenados en OSS.",
		"fr": "Les buckets OSS doivent avoir le chiffrement côté serveur activé pour protéger les données au repos. Le chiffrement côté serveur utilise KMS ou AES256 pour chiffrer les données stockées dans OSS.",
		"pt": "Buckets OSS devem ter criptografia do lado do servidor habilitada para proteger dados em repouso. A criptografia do lado do servidor usa KMS ou AES256 para criptografar dados armazenados no OSS."
	},
	"reason": {
		"en": "The OSS bucket does not have server-side encryption enabled, which may expose sensitive data to unauthorized access.",
		"zh": "OSS 存储空间未开启服务端加密，可能导致敏感数据暴露给未授权访问。",
		"ja": "OSS バケットでサーバー側暗号化が有効になっていないため、機密データが不正アクセスにさらされる可能性があります。",
		"de": "Der OSS-Bucket hat keine serverseitige Verschlüsselung aktiviert, was sensible Daten unbefugtem Zugriff aussetzen kann.",
		"es": "El bucket OSS no tiene cifrado del lado del servidor habilitado, lo que puede exponer datos sensibles a acceso no autorizado.",
		"fr": "Le bucket OSS n'a pas le chiffrement côté serveur activé, ce qui peut exposer des données sensibles à un accès non autorisé.",
		"pt": "O bucket OSS não tem criptografia do lado do servidor habilitada, o que pode expor dados sensíveis a acesso não autorizado."
	},
	"recommendation": {
		"en": "Enable server-side encryption for the OSS bucket by configuring the ServerSideEncryptionConfiguration property with SSEAlgorithm set to KMS, AES256, or SM4.",
		"zh": "通过配置 ServerSideEncryptionConfiguration 属性并将 SSEAlgorithm 设置为 KMS、AES256 或 SM4，为 OSS 存储空间启用服务端加密。",
		"ja": "SSEAlgorithm を KMS、AES256、または SM4 に設定して ServerSideEncryptionConfiguration プロパティを設定することで、OSS バケットのサーバー側暗号化を有効にします。",
		"de": "Aktivieren Sie die serverseitige Verschlüsselung für den OSS-Bucket, indem Sie die ServerSideEncryptionConfiguration-Eigenschaft mit SSEAlgorithm auf KMS, AES256 oder SM4 konfigurieren.",
		"es": "Habilite el cifrado del lado del servidor para el bucket OSS configurando la propiedad ServerSideEncryptionConfiguration con SSEAlgorithm establecido en KMS, AES256 o SM4.",
		"fr": "Activez le chiffrement côté serveur pour le bucket OSS en configurant la propriété ServerSideEncryptionConfiguration avec SSEAlgorithm défini sur KMS, AES256 ou SM4.",
		"pt": "Habilite criptografia do lado do servidor para o bucket OSS configurando a propriedade ServerSideEncryptionConfiguration com SSEAlgorithm definido como KMS, AES256 ou SM4."
	},
	"resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
	not has_server_side_encryption(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "ServerSideEncryptionConfiguration"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}

has_server_side_encryption(resource) if {
	helpers.has_property(resource, "ServerSideEncryptionConfiguration")
	sse_config := resource.Properties.ServerSideEncryptionConfiguration
	sse_config.SSEAlgorithm in ["KMS", "AES256", "SM4"]
}
