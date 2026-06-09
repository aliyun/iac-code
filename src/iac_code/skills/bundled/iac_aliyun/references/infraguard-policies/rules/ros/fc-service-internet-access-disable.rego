package infraguard.rules.aliyun.fc_service_internet_access_disable

import rego.v1

import data.infraguard.helpers

# Rule metadata
rule_meta := {
	"id": "fc-service-internet-access-disable",
	"severity": "medium",
	"name": {
		"en": "FC Service Internet Access Disabled",
		"zh": "FC 服务禁用公网访问",
		"ja": "FC サービスインターネットアクセスが無効",
		"de": "FC-Service Internetzugriff deaktiviert",
		"es": "Acceso a Internet del Servicio FC Deshabilitado",
		"fr": "Accès Internet du Service FC Désactivé",
		"pt": "Acesso à Internet do Serviço FC Desabilitado"
	},
	"description": {
		"en": "Ensures that the Function Compute service has internet access disabled when it should only access internal resources.",
		"zh": "确保函数计算服务在仅需访问内网资源时已禁用公网访问。",
		"ja": "関数計算サービスが内部リソースにのみアクセスする必要がある場合に、インターネットアクセスが無効になっていることを確認します。",
		"de": "Stellt sicher, dass der Function Compute-Service Internetzugriff deaktiviert hat, wenn er nur auf interne Ressourcen zugreifen sollte.",
		"es": "Garantiza que el servicio Function Compute tenga acceso a Internet deshabilitado cuando solo deba acceder a recursos internos.",
		"fr": "Garantit que le service Function Compute a l'accès Internet désactivé lorsqu'il ne doit accéder qu'aux ressources internes.",
		"pt": "Garante que o serviço Function Compute tenha acesso à Internet desabilitado quando deve acessar apenas recursos internos."
	},
	"reason": {
		"en": "Disabling internet access for FC services reduces the attack surface and potential for data exfiltration.",
		"zh": "为 FC 服务禁用公网访问可减少攻击面和潜在的数据泄露风险。",
		"ja": "FC サービスのインターネットアクセスを無効にすることで、攻撃面とデータ漏洩の可能性を減らします。",
		"de": "Das Deaktivieren des Internetzugriffs für FC-Services reduziert die Angriffsfläche und das Potenzial für Datenexfiltration.",
		"es": "Deshabilitar el acceso a Internet para los servicios FC reduce la superficie de ataque y el potencial de exfiltración de datos.",
		"fr": "Désactiver l'accès Internet pour les services FC réduit la surface d'attaque et le potentiel d'exfiltration de données.",
		"pt": "Desabilitar o acesso à Internet para serviços FC reduz a superfície de ataque e o potencial de exfiltração de dados."
	},
	"recommendation": {
		"en": "Disable internet access for the Function Compute service.",
		"zh": "为函数计算服务禁用公网访问。",
		"ja": "関数計算サービスのインターネットアクセスを無効にします。",
		"de": "Deaktivieren Sie den Internetzugriff für den Function Compute-Service.",
		"es": "Deshabilite el acceso a Internet para el servicio Function Compute.",
		"fr": "Désactivez l'accès Internet pour le service Function Compute.",
		"pt": "Desabilite o acesso à Internet para o serviço Function Compute."
	},
	"resource_types": ["ALIYUN::FC::Service"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::FC::Service")
	helpers.get_property(resource, "InternetAccess", true)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "InternetAccess"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
