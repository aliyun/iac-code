package infraguard.rules.aliyun.cr_repository_image_scanning_enabled

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "cr-repository-image-scanning-enabled",
	"severity": "high",
	"name": {
		"en": "CR Instance Image Scanning Enabled",
		"zh": "为容器镜像实例开启安全扫描",
		"ja": "CR インスタンスイメージスキャンが有効",
		"de": "CR-Instanz Bild-Scanning aktiviert",
		"es": "Escaneo de Imagen de Instancia CR Habilitado",
		"fr": "Balayage d'Image d'Instance CR Activé",
		"pt": "Varredura de Imagem de Instância CR Habilitada"
	},
	"description": {
		"en": "Ensures Container Registry instances have image scanning enabled for security vulnerability detection.",
		"zh": "确保容器镜像实例开启了镜像安全扫描功能以检测安全漏洞。",
		"ja": "コンテナレジストリインスタンスでセキュリティ脆弱性検出のためにイメージスキャンが有効になっていることを確認します。",
		"de": "Stellt sicher, dass Container Registry-Instanzen Bild-Scanning für die Erkennung von Sicherheitslücken aktiviert haben.",
		"es": "Garantiza que las instancias de Container Registry tengan escaneo de imágenes habilitado para la detección de vulnerabilidades de seguridad.",
		"fr": "Garantit que les instances Container Registry ont le balayage d'images activé pour la détection des vulnérabilités de sécurité.",
		"pt": "Garante que as instâncias do Container Registry tenham varredura de imagem habilitada para detecção de vulnerabilidades de segurança."
	},
	"reason": {
		"en": "Image scanning helps identify and prevent deployment of vulnerable container images.",
		"zh": "镜像扫描有助于识别和防止部署有漏洞的容器镜像。",
		"ja": "イメージスキャンは、脆弱なコンテナイメージの展開を識別および防止するのに役立ちます。",
		"de": "Bild-Scanning hilft dabei, die Bereitstellung anfälliger Container-Images zu identifizieren und zu verhindern.",
		"es": "El escaneo de imágenes ayuda a identificar y prevenir el despliegue de imágenes de contenedor vulnerables.",
		"fr": "Le balayage d'images aide à identifier et empêcher le déploiement d'images de conteneurs vulnérables.",
		"pt": "A varredura de imagem ajuda a identificar e prevenir a implantação de imagens de contêiner vulneráveis."
	},
	"recommendation": {
		"en": "Enable image scanning for the Container Registry instance.",
		"zh": "为容器镜像实例启用镜像扫描功能。",
		"ja": "コンテナレジストリインスタンスのイメージスキャンを有効にします。",
		"de": "Aktivieren Sie Bild-Scanning für die Container Registry-Instanz.",
		"es": "Habilite el escaneo de imágenes para la instancia de Container Registry.",
		"fr": "Activez le balayage d'images pour l'instance Container Registry.",
		"pt": "Habilite a varredura de imagem para a instância do Container Registry."
	},
	"resource_types": ["ALIYUN::CR::Instance"]
}

is_compliant(resource) if {
	# Check ImageScanner property
	image_scanner := helpers.get_property(resource, "ImageScanner", "")
	count(image_scanner) > 0
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::CR::Instance")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "ImageScanner"],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
