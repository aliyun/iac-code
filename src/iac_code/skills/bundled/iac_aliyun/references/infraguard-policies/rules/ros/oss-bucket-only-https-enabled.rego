package infraguard.rules.aliyun.oss_bucket_only_https_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "oss-bucket-only-https-enabled",
	"severity": "high",
	"name": {
		"en": "OSS Bucket Only HTTPS Enabled",
		"zh": "OSS 存储桶开启仅允许 HTTPS 访问",
		"ja": "OSS バケットで HTTPS のみが有効",
		"de": "OSS-Bucket nur HTTPS aktiviert",
		"es": "Solo HTTPS de Bucket OSS Habilitado",
		"fr": "Seul HTTPS de Bucket OSS Activé",
		"pt": "Apenas HTTPS de Bucket OSS Habilitado"
	},
	"description": {
		"en": "OSS bucket should have a policy that denies non-HTTPS requests to ensure data transport security.",
		"zh": "OSS 存储桶应配置仅允许 HTTPS 访问的策略，以确保数据传输安全。",
		"ja": "OSS バケットは、データ転送のセキュリティを確保するために、非 HTTPS リクエストを拒否するポリシーを持つ必要があります。",
		"de": "OSS-Bucket sollte eine Richtlinie haben, die Nicht-HTTPS-Anfragen ablehnt, um die Datentransportsicherheit zu gewährleisten.",
		"es": "El bucket OSS debe tener una política que niegue las solicitudes que no sean HTTPS para garantizar la seguridad del transporte de datos.",
		"fr": "Le bucket OSS doit avoir une politique qui refuse les requêtes non-HTTPS pour assurer la sécurité du transport des données.",
		"pt": "O bucket OSS deve ter uma política que negue solicitações não HTTPS para garantir a segurança do transporte de dados."
	},
	"reason": {
		"en": "The OSS bucket allows non-HTTPS requests, which may lead to data interception or tampering during transport.",
		"zh": "OSS 存储桶允许非 HTTPS 请求，可能导致数据在传输过程中被窃听或篡改。",
		"ja": "OSS バケットが非 HTTPS リクエストを許可しているため、転送中にデータが傍受または改ざんされる可能性があります。",
		"de": "Der OSS-Bucket erlaubt Nicht-HTTPS-Anfragen, was zu Datenabfangen oder Manipulation während des Transports führen kann.",
		"es": "El bucket OSS permite solicitudes que no sean HTTPS, lo que puede llevar a la interceptación o manipulación de datos durante el transporte.",
		"fr": "Le bucket OSS autorise les requêtes non-HTTPS, ce qui peut entraîner une interception ou une falsification des données pendant le transport.",
		"pt": "O bucket OSS permite solicitações não HTTPS, o que pode levar à interceptação ou adulteração de dados durante o transporte."
	},
	"recommendation": {
		"en": "Configure a bucket policy that denies requests where 'acs:SecureTransport' is false.",
		"zh": "配置存储桶策略，拒绝 'acs:SecureTransport' 为 false 的请求。",
		"ja": "'acs:SecureTransport' が false のリクエストを拒否するバケットポリシーを設定します。",
		"de": "Konfigurieren Sie eine Bucket-Richtlinie, die Anfragen ablehnt, bei denen 'acs:SecureTransport' false ist.",
		"es": "Configure una política de bucket que niegue las solicitudes donde 'acs:SecureTransport' es false.",
		"fr": "Configurez une politique de bucket qui refuse les requêtes où 'acs:SecureTransport' est false.",
		"pt": "Configure uma política de bucket que negue solicitações onde 'acs:SecureTransport' é false."
	},
	"resource_types": ["ALIYUN::OSS::Bucket"]
}

# Check if the bucket has a policy that enforces HTTPS
is_only_https_enabled(resource) if {
	policy := helpers.get_property(resource, "Policy", {})
	statements := object.get(policy, "Statement", [])
	some statement in statements
	statement.Effect == "Deny"

	# Check for SecureTransport condition
	condition := object.get(statement, "Condition", {})
	bool_cond := object.get(condition, "Bool", {})
	secure_transport := object.get(bool_cond, "acs:SecureTransport", null)
	has_false_value(secure_transport)
}

has_false_value(val) if {
	val == "false"
}

has_false_value(val) if {
	val == false
}

has_false_value(val) if {
	is_array(val)
	some item in val
	item_is_false(item)
}

item_is_false(v) if {
	v == "false"
}

item_is_false(v) if {
	v == false
}

deny contains result if {
	some name, resource in helpers.resources_by_types(rule_meta.resource_types)
	not is_only_https_enabled(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "Policy"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
