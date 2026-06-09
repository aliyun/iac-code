package infraguard.rules.aliyun.rds_public_connection_and_any_ip_access_check

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "rds-public-connection-and-any-ip-access-check",
	"severity": "high",
	"name": {
		"en": "RDS Public Connection and Any IP Access Check",
		"zh": "开启公网 IP 的 RDS 实例白名单未对所有来源开放",
		"ja": "RDS のパブリック接続および任意の IP アクセスチェック",
		"de": "RDS öffentliche Verbindung und beliebige IP-Zugriff-Prüfung",
		"es": "Verificación de Conexión Pública y Acceso de Cualquier IP de RDS",
		"fr": "Vérification de Connexion Publique et d'Accès de N'importe Quelle IP RDS",
		"pt": "Verificação de Conexão Pública e Acesso de Qualquer IP RDS"
	},
	"description": {
		"en": "Ensures that RDS instances with public connections do not have a whitelist open to all IPs.",
		"zh": "确保开启公网 IP 的 RDS 实例白名单未设置为对所有来源 IP 开放。",
		"ja": "パブリック接続を持つ RDS インスタンスがすべての IP に開放されたホワイトリストを持っていないことを確認します。",
		"de": "Stellt sicher, dass RDS-Instanzen mit öffentlichen Verbindungen keine Whitelist haben, die für alle IPs geöffnet ist.",
		"es": "Garantiza que las instancias RDS con conexiones públicas no tengan una lista blanca abierta a todas las IPs.",
		"fr": "Garantit que les instances RDS avec connexions publiques n'ont pas de liste blanche ouverte à toutes les IPs.",
		"pt": "Garante que instâncias RDS com conexões públicas não tenham uma lista branca aberta para todos os IPs."
	},
	"reason": {
		"en": "An open whitelist combined with a public connection exposes the database to the internet, creating a high security risk.",
		"zh": "公网连接配合开放白名单会将数据库暴露在互联网上，造成极高的安全风险。",
		"ja": "オープンホワイトリストとパブリック接続の組み合わせにより、データベースがインターネットに公開され、高いセキュリティリスクが生じます。",
		"de": "Eine offene Whitelist in Kombination mit einer öffentlichen Verbindung setzt die Datenbank dem Internet aus und schafft ein hohes Sicherheitsrisiko.",
		"es": "Una lista blanca abierta combinada con una conexión pública expone la base de datos a internet, creando un alto riesgo de seguridad.",
		"fr": "Une liste blanche ouverte combinée à une connexion publique expose la base de données à Internet, créant un risque de sécurité élevé.",
		"pt": "Uma lista branca aberta combinada com uma conexão pública expõe o banco de dados à internet, criando um alto risco de segurança."
	},
	"recommendation": {
		"en": "Disable public connection or restrict the IP whitelist for the RDS instance.",
		"zh": "禁用 RDS 实例的公网连接或限制 IP 白名单。",
		"ja": "RDS インスタンスのパブリック接続を無効にするか、IP ホワイトリストを制限します。",
		"de": "Deaktivieren Sie die öffentliche Verbindung oder beschränken Sie die IP-Whitelist für die RDS-Instanz.",
		"es": "Deshabilite la conexión pública o restrinja la lista blanca de IP para la instancia RDS.",
		"fr": "Désactivez la connexion publique ou restreignez la liste blanche IP pour l'instance RDS.",
		"pt": "Desabilite a conexão pública ou restrinja a lista branca de IP para a instância RDS."
	},
	"resource_types": ["ALIYUN::RDS::DBInstance"]
}

is_compliant(resource) if {
	# If public connection is not enabled, it's compliant
	not helpers.is_true(helpers.get_property(resource, "AllocatePublicConnection", false))
}

is_compliant(resource) if {
	# If public connection is enabled, check the whitelist
	helpers.is_true(helpers.get_property(resource, "AllocatePublicConnection", false))
	whitelist_str := helpers.get_property(resource, "SecurityIPList", "")
	whitelist := split(whitelist_str, ",")
	not has_open_cidr(whitelist)
}

has_open_cidr(whitelist) if {
	some cidr in whitelist
	helpers.is_public_cidr(trim_space(cidr))
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
	not is_compliant(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SecurityIPList"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
