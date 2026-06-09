package infraguard.rules.aliyun.vpc_flow_logs_enabled

import data.infraguard.helpers
import rego.v1

rule_meta := {
	"id": "vpc-flow-logs-enabled",
	"severity": "medium",
	"name": {
		"en": "VPC Flow Logs Enabled",
		"zh": "VPC 开启流日志",
		"ja": "VPC フローログが有効",
		"de": "VPC-Flussprotokolle aktiviert",
		"es": "Registros de Flujo VPC Habilitados",
		"fr": "Journaux de Flux VPC Activés",
		"pt": "Logs de Fluxo VPC Habilitados"
	},
	"description": {
		"en": "Ensures VPC flow logs are enabled for monitoring network traffic.",
		"zh": "确保 VPC 开启了流日志，以便监控网络流量。",
		"ja": "ネットワークトラフィックを監視するために VPC フローログが有効になっていることを確認します。",
		"de": "Stellt sicher, dass VPC-Flussprotokolle für die Überwachung des Netzwerkverkehrs aktiviert sind.",
		"es": "Garantiza que los registros de flujo VPC estén habilitados para monitorear el tráfico de red.",
		"fr": "Garantit que les journaux de flux VPC sont activés pour surveiller le trafic réseau.",
		"pt": "Garante que os logs de fluxo VPC estejam habilitados para monitorar tráfego de rede."
	},
	"reason": {
		"en": "Flow logs provide visibility into network traffic patterns and help in security auditing.",
		"zh": "流日志提供了网络流量模式的可见性，有助于安全审计。",
		"ja": "フローログはネットワークトラフィックパターンの可視性を提供し、セキュリティ監査に役立ちます。",
		"de": "Flussprotokolle bieten Einblicke in Netzwerkverkehrsmuster und helfen bei Sicherheitsaudits.",
		"es": "Los registros de flujo proporcionan visibilidad sobre los patrones de tráfico de red y ayudan en la auditoría de seguridad.",
		"fr": "Les journaux de flux fournissent une visibilité sur les modèles de trafic réseau et aident à l'audit de sécurité.",
		"pt": "Logs de fluxo fornecem visibilidade sobre padrões de tráfego de rede e ajudam na auditoria de segurança."
	},
	"recommendation": {
		"en": "Add ALIYUN::VPC::FlowLog resource to enable flow logs for the VPC.",
		"zh": "添加 ALIYUN::VPC::FlowLog 资源以为 VPC 开启流日志。",
		"ja": "ALIYUN::VPC::FlowLog リソースを追加して、VPC のフローログを有効にします。",
		"de": "Fügen Sie die ALIYUN::VPC::FlowLog-Ressource hinzu, um Flussprotokolle für das VPC zu aktivieren.",
		"es": "Agregue el recurso ALIYUN::VPC::FlowLog para habilitar los registros de flujo para el VPC.",
		"fr": "Ajoutez la ressource ALIYUN::VPC::FlowLog pour activer les journaux de flux pour le VPC.",
		"pt": "Adicione o recurso ALIYUN::VPC::FlowLog para habilitar logs de fluxo para o VPC."
	},
	"resource_types": ["ALIYUN::ECS::VPC"]
}

# Cross-resource check: is there a FlowLog resource for this VPC?
has_flow_log(vpc_id) if {
	some name, res in helpers.resources_by_type("ALIYUN::VPC::FlowLog")
	helpers.is_referencing(helpers.get_property(res, "ResourceId", ""), vpc_id)
}

deny contains result if {
	some vpc_id, resource in helpers.resources_by_type("ALIYUN::ECS::VPC")
	not has_flow_log(vpc_id)
	result := {
		"id": rule_meta.id,
		"resource_id": vpc_id,
		"violation_path": [],
		"meta": {"severity": rule_meta.severity, "reason": rule_meta.reason, "recommendation": rule_meta.recommendation},
	}
}
