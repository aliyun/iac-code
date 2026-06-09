package infraguard.rules.aliyun.ecs_security_group_not_internet_cidr_access

import rego.v1

import data.infraguard.helpers

# Rule metadata with i18n support
rule_meta := {
	"id": "ecs-security-group-not-internet-cidr-access",
	"severity": "high",
	"name": {
		"en": "Security Group Ingress Source IP Not Include Public IP",
		"zh": "安全组入网设置允许的来源 IP 不包含公网 IP",
		"ja": "セキュリティグループイングレスのソース IP にパブリック IP が含まれていない",
		"de": "Sicherheitsgruppe Ingress-Quell-IP enthält keine öffentliche IP",
		"es": "IP de Origen de Ingress de Grupo de Seguridad No Incluye IP Pública",
		"fr": "IP Source d'Ingress du Groupe de Sécurité N'Inclut Pas d'IP Publique",
		"pt": "IP de Origem de Ingresso de Grupo de Segurança Não Inclui IP Público"
	},
	"description": {
		"en": "Security group ingress rules with accept policy should not have source IP containing public internet IPs.",
		"zh": "安全组入网方向授权策略为允许的来源 IP 地址段不包含公网 IP，视为合规。",
		"ja": "許可ポリシーを持つセキュリティグループのイングレスルールは、パブリックインターネット IP を含むソース IP を持つべきではありません。",
		"de": "Sicherheitsgruppen-Ingress-Regeln mit Accept-Richtlinie sollten keine Quell-IP enthalten, die öffentliche Internet-IPs enthält.",
		"es": "Las reglas de ingreso del grupo de seguridad con política de aceptación no deben tener IP de origen que contenga IPs públicas de internet.",
		"fr": "Les règles d'ingress du groupe de sécurité avec politique d'acceptation ne doivent pas avoir d'IP source contenant des IPs Internet publiques.",
		"pt": "As regras de ingresso do grupo de segurança com política de aceitação não devem ter IP de origem contendo IPs públicos da internet."
	},
	"reason": {
		"en": "The security group has an ingress rule that allows access from public internet IP addresses, which may expose the resources to external attacks.",
		"zh": "安全组有一条入网规则允许从公网 IP 地址访问，可能将资源暴露给外部攻击。",
		"ja": "セキュリティグループにパブリックインターネット IP アドレスからのアクセスを許可するイングレスルールがあり、リソースが外部攻撃にさらされる可能性があります。",
		"de": "Die Sicherheitsgruppe hat eine Ingress-Regel, die Zugriff von öffentlichen Internet-IP-Adressen erlaubt, was die Ressourcen externen Angriffen aussetzen kann.",
		"es": "El grupo de seguridad tiene una regla de ingreso que permite el acceso desde direcciones IP públicas de internet, lo que puede exponer los recursos a ataques externos.",
		"fr": "Le groupe de sécurité a une règle d'ingress qui autorise l'accès depuis des adresses IP Internet publiques, ce qui peut exposer les ressources à des attaques externes.",
		"pt": "O grupo de segurança tem uma regra de ingresso que permite acesso de endereços IP públicos da internet, o que pode expor os recursos a ataques externos."
	},
	"recommendation": {
		"en": "Restrict ingress source IP to private network ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) unless public internet access is explicitly required.",
		"zh": "将入网来源 IP 限制为私有网络范围（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16），除非确实需要公网访问。",
		"ja": "パブリックインターネットアクセスが明示的に必要な場合を除き、イングレスソース IP をプライベートネットワーク範囲（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）に制限します。",
		"de": "Beschränken Sie die Ingress-Quell-IP auf private Netzwerkbereiche (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), es sei denn, öffentlicher Internetzugriff ist ausdrücklich erforderlich.",
		"es": "Restrinja la IP de origen de ingreso a rangos de red privada (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) a menos que se requiera explícitamente acceso público a internet.",
		"fr": "Restreignez l'IP source d'ingress aux plages de réseau privé (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) sauf si l'accès Internet public est explicitement requis.",
		"pt": "Restrinja o IP de origem de ingresso a intervalos de rede privada (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), a menos que o acesso público à internet seja explicitamente necessário."
	},
	"resource_types": ["ALIYUN::ECS::SecurityGroup", "ALIYUN::ECS::SecurityGroupIngress", "ALIYUN::ECS::SecurityGroupIngresses"]
}

# Check if a rule allows access from internet CIDR
is_internet_source_rule(rule) if {
	# Source CIDR is a public internet IP
	cidr := rule.SourceCidrIp
	cidr != ""
	helpers.is_internet_cidr(cidr)

	# Policy is accept (default)
	object.get(rule, "Policy", "accept") == "accept"
}

# Check SecurityGroup resource for ingress rules
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
	some i, rule in resource.Properties.SecurityGroupIngress
	is_internet_source_rule(rule)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SecurityGroupIngress", format_int(i, 10), "SourceCidrIp"],
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
		"SourceCidrIp": object.get(props, "SourceCidrIp", ""),
		"Policy": object.get(props, "Policy", "accept"),
	}
	is_internet_source_rule(ingress_rule)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "SourceCidrIp"],
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
		"SourceCidrIp": object.get(perm, "SourceCidrIp", ""),
		"Policy": object.get(perm, "Policy", "accept"),
	}
	is_internet_source_rule(ingress_rule)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "Permissions", format_int(i, 10), "SourceCidrIp"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
