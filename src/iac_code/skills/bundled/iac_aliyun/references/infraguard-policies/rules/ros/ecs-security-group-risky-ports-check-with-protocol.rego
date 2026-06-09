package infraguard.rules.aliyun.ecs_security_group_risky_ports_check_with_protocol

import rego.v1

import data.infraguard.helpers

# Rule metadata with i18n support
rule_meta := {
	"id": "ecs-security-group-risky-ports-check-with-protocol",
	"severity": "high",
	"name": {
		"en": "Security Group Risky Ports Check with Protocol",
		"zh": "安全组指定协议不允许对全部网段开启风险端口",
		"ja": "プロトコル付きセキュリティグループのリスクポートチェック",
		"de": "Sicherheitsgruppe riskante Ports-Prüfung mit Protokoll",
		"es": "Verificación de Puertos de Riesgo del Grupo de Seguridad con Protocolo",
		"fr": "Vérification des Ports à Risque du Groupe de Sécurité avec Protocole",
		"pt": "Verificação de Portas de Risco do Grupo de Segurança com Protocolo"
	},
	"description": {
		"en": "When security group ingress source is set to 0.0.0.0/0, the port range should not include risky ports (22, 3389) for specified protocols (TCP/UDP), to reduce the risk of brute force attacks.",
		"zh": "当安全组入网网段设置为 0.0.0.0/0 时，指定协议的端口范围不包含指定风险端口，降低服务器登录密码被暴力破解风险，视为合规。默认检测风险端口为 22、3389。",
		"ja": "セキュリティグループのイングレスソースが 0.0.0.0/0 に設定されている場合、ブルートフォース攻撃のリスクを低減するために、指定されたプロトコル（TCP/UDP）のポート範囲にリスクポート（22、3389）を含めるべきではありません。",
		"de": "Wenn die Sicherheitsgruppen-Eingangsquelle auf 0.0.0.0/0 gesetzt ist, sollte der Portbereich keine riskanten Ports (22, 3389) für angegebene Protokolle (TCP/UDP) enthalten, um das Risiko von Brute-Force-Angriffen zu reduzieren.",
		"es": "Cuando la fuente de ingreso del grupo de seguridad se establece en 0.0.0.0/0, el rango de puertos no debe incluir puertos de riesgo (22, 3389) para protocolos especificados (TCP/UDP), para reducir el riesgo de ataques de fuerza bruta.",
		"fr": "Lorsque la source d'ingress du groupe de sécurité est définie sur 0.0.0.0/0, la plage de ports ne doit pas inclure les ports à risque (22, 3389) pour les protocoles spécifiés (TCP/UDP), afin de réduire le risque d'attaques par force brute.",
		"pt": "Quando a origem de entrada do grupo de segurança é definida como 0.0.0.0/0, o intervalo de portas não deve incluir portas de risco (22, 3389) para protocolos especificados (TCP/UDP), para reduzir o risco de ataques de força bruta."
	},
	"reason": {
		"en": "The security group allows access to risky ports (SSH:22, RDP:3389) from all sources (0.0.0.0/0), which increases the risk of brute force password attacks.",
		"zh": "安全组允许从所有来源（0.0.0.0/0）访问风险端口（SSH:22、RDP:3389），增加了暴力破解密码的风险。",
		"ja": "セキュリティグループがすべてのソース（0.0.0.0/0）からのリスクポート（SSH:22、RDP:3389）へのアクセスを許可しているため、ブルートフォースパスワード攻撃のリスクが増加します。",
		"de": "Die Sicherheitsgruppe erlaubt Zugriff auf riskante Ports (SSH:22, RDP:3389) von allen Quellen (0.0.0.0/0), was das Risiko von Brute-Force-Passwortangriffen erhöht.",
		"es": "El grupo de seguridad permite acceso a puertos de riesgo (SSH:22, RDP:3389) desde todas las fuentes (0.0.0.0/0), lo que aumenta el riesgo de ataques de fuerza bruta en contraseñas.",
		"fr": "Le groupe de sécurité autorise l'accès aux ports à risque (SSH:22, RDP:3389) depuis toutes les sources (0.0.0.0/0), ce qui augmente le risque d'attaques par force brute sur les mots de passe.",
		"pt": "O grupo de segurança permite acesso a portas de risco (SSH:22, RDP:3389) de todas as fontes (0.0.0.0/0), o que aumenta o risco de ataques de força bruta em senhas."
	},
	"recommendation": {
		"en": "Restrict access to ports 22 (SSH) and 3389 (RDP) by limiting the source CIDR to specific trusted IP ranges instead of 0.0.0.0/0.",
		"zh": "限制对端口 22（SSH）和 3389（RDP）的访问，将来源 CIDR 限制为特定的可信 IP 范围，而不是 0.0.0.0/0。",
		"ja": "ソース CIDR を 0.0.0.0/0 ではなく、特定の信頼できる IP 範囲に制限することで、ポート 22（SSH）と 3389（RDP）へのアクセスを制限します。",
		"de": "Beschränken Sie den Zugriff auf Ports 22 (SSH) und 3389 (RDP), indem Sie das Quell-CIDR auf spezifische vertrauenswürdige IP-Bereiche anstelle von 0.0.0.0/0 beschränken.",
		"es": "Restrinja el acceso a los puertos 22 (SSH) y 3389 (RDP) limitando el CIDR de origen a rangos de IP confiables específicos en lugar de 0.0.0.0/0.",
		"fr": "Restreignez l'accès aux ports 22 (SSH) et 3389 (RDP) en limitant le CIDR source à des plages d'IP de confiance spécifiques au lieu de 0.0.0.0/0.",
		"pt": "Restrinja o acesso às portas 22 (SSH) e 3389 (RDP) limitando o CIDR de origem a intervalos de IP confiáveis específicos em vez de 0.0.0.0/0."
	},
	"resource_types": ["ALIYUN::ECS::SecurityGroup", "ALIYUN::ECS::SecurityGroupIngress", "ALIYUN::ECS::SecurityGroupIngresses"]
}

# Risky ports that should not be exposed to all sources
risky_ports := [22, 3389]

# Protocols to check (TCP and UDP are relevant for SSH and RDP)
risky_protocols := ["tcp", "udp"]

# Check if a rule exposes risky ports from public sources
is_risky_public_rule(rule) if {
	# Source is 0.0.0.0/0 (all IPv4)
	rule.SourceCidrIp == "0.0.0.0/0"

	# Protocol is TCP, UDP, or ALL
	lower(rule.IpProtocol) in risky_protocols

	# Policy is accept (default)
	object.get(rule, "Policy", "accept") == "accept"

	# Check if any risky port is in the range
	some port in risky_ports
	helpers.port_in_range(port, rule.PortRange)
}

is_risky_public_rule(rule) if {
	# Source is ::/0 (all IPv6)
	rule.Ipv6SourceCidrIp == "::/0"

	# Protocol is TCP, UDP, or ALL
	lower(rule.IpProtocol) in risky_protocols

	# Policy is accept (default)
	object.get(rule, "Policy", "accept") == "accept"

	# Check if any risky port is in the range
	some port in risky_ports
	helpers.port_in_range(port, rule.PortRange)
}

# Check SecurityGroup resource for ingress rules
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
	some i, rule in resource.Properties.SecurityGroupIngress
	is_risky_public_rule(rule)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SecurityGroupIngress", format_int(i, 10)],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}

# Check SecurityGroupIngress resource
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroupIngress")
	props := resource.Properties
	ingress_rule := {
		"IpProtocol": props.IpProtocol,
		"PortRange": props.PortRange,
		"SourceCidrIp": object.get(props, "SourceCidrIp", ""),
		"Ipv6SourceCidrIp": object.get(props, "Ipv6SourceCidrIp", ""),
		"Policy": object.get(props, "Policy", "accept"),
	}
	is_risky_public_rule(ingress_rule)
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

# Check SecurityGroupIngresses resource
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroupIngresses")
	some i, perm in resource.Properties.Permissions
	ingress_rule := {
		"IpProtocol": perm.IpProtocol,
		"PortRange": perm.PortRange,
		"SourceCidrIp": object.get(perm, "SourceCidrIp", ""),
		"Ipv6SourceCidrIp": object.get(perm, "Ipv6SourceCidrIp", ""),
		"Policy": object.get(perm, "Policy", "accept"),
	}
	is_risky_public_rule(ingress_rule)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "Permissions", format_int(i, 10)],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
