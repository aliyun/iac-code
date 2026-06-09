package infraguard.rules.aliyun.api_gateway_api_internet_request_https

import rego.v1

import data.infraguard.helpers

# Rule metadata
rule_meta := {
	"id": "api-gateway-api-internet-request-https",
	"severity": "medium",
	"name": {
		"en": "API Gateway Internet Request HTTPS Enabled",
		"zh": "API 网关公网请求开启 HTTPS",
		"ja": "API ゲートウェイのインターネットリクエストで HTTPS が有効",
		"de": "API Gateway Internet-Anfrage HTTPS aktiviert",
		"es": "HTTPS de Solicitud de Internet del Gateway de API Habilitado",
		"fr": "HTTPS de Requête Internet du Gateway API Activé",
		"pt": "HTTPS de Solicitação de Internet do Gateway de API Habilitado"
	},
	"description": {
		"en": "Ensures that API Gateway APIs exposed to the internet use HTTPS protocol.",
		"zh": "确保暴露给公网的 API 网关 API 使用 HTTPS 协议。",
		"ja": "インターネットに公開された API ゲートウェイ API が HTTPS プロトコルを使用していることを確認します。",
		"de": "Stellt sicher, dass API Gateway APIs, die dem Internet ausgesetzt sind, das HTTPS-Protokoll verwenden.",
		"es": "Garantiza que las APIs del Gateway de API expuestas a internet usen el protocolo HTTPS.",
		"fr": "Garantit que les APIs du Gateway API exposées à Internet utilisent le protocole HTTPS.",
		"pt": "Garante que as APIs do Gateway de API expostas à internet usem o protocolo HTTPS."
	},
	"reason": {
		"en": "HTTPS ensures data confidentiality and integrity during transmission over the internet.",
		"zh": "HTTPS 可确保在公网传输期间数据的机密性和完整性。",
		"ja": "HTTPS により、インターネット経由での送信中にデータの機密性と整合性が確保されます。",
		"de": "HTTPS gewährleistet Datenvertraulichkeit und Integrität während der Übertragung über das Internet.",
		"es": "HTTPS garantiza la confidencialidad e integridad de los datos durante la transmisión por internet.",
		"fr": "HTTPS assure la confidentialité et l'intégrité des données pendant la transmission sur Internet.",
		"pt": "O HTTPS garante confidencialidade e integridade dos dados durante a transmissão pela internet."
	},
	"recommendation": {
		"en": "Configure the API Gateway API to require HTTPS for internet requests.",
		"zh": "配置 API 网关 API，要求公网请求使用 HTTPS。",
		"ja": "API ゲートウェイ API を設定して、インターネットリクエストに HTTPS を要求します。",
		"de": "Konfigurieren Sie die API Gateway API so, dass HTTPS für Internet-Anfragen erforderlich ist.",
		"es": "Configure la API del Gateway de API para requerir HTTPS para solicitudes de internet.",
		"fr": "Configurez l'API du Gateway API pour exiger HTTPS pour les requêtes Internet.",
		"pt": "Configure a API do Gateway de API para exigir HTTPS para solicitações de internet."
	},
	"resource_types": ["ALIYUN::ApiGateway::Api"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ApiGateway::Api")

	# Conceptual check for protocol
	proto := helpers.get_property(resource, "RequestConfig", {"RequestProtocol": "HTTP"}).RequestProtocol
	proto == "HTTP"
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "RequestConfig", "RequestProtocol"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
