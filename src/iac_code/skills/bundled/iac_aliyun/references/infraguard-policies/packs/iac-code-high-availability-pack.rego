package infraguard.packs.aliyun.iac_code_high_availability

import rego.v1

pack_meta := {
	"id": "iac-code-high-availability",
	"name": {
		"en": "IaC Code High Availability Pack",
		"zh": "IaC Code 高可用合规包",
		"ja": "IaC Code 高可用性パック",
		"de": "IaC Code Hochverfügbarkeitspaket",
		"es": "Paquete de alta disponibilidad de IaC Code",
		"fr": "Pack haute disponibilité IaC Code",
		"pt": "Pacote de alta disponibilidade do IaC Code",
	},
	"description": {
		"en": "Checks Alibaba Cloud ROS resources for multi-zone placement, load balancer failover, baseline replica counts, and zone-redundant storage.",
		"zh": "检查阿里云 ROS 资源的多可用区部署、负载均衡故障转移、基线副本数和同城冗余存储配置。",
		"ja": "Alibaba Cloud ROS リソースのマルチゾーン配置、ロードバランサーフェイルオーバー、基準レプリカ数、ゾーン冗長ストレージを確認します。",
		"de": "Prüft Alibaba Cloud ROS-Ressourcen auf Multi-Zone-Platzierung, Load-Balancer-Failover, Basis-Replikatanzahlen und zonenredundanten Speicher.",
		"es": "Comprueba recursos ROS de Alibaba Cloud para ubicación multi-zona, failover de balanceadores, recuentos base de réplicas y almacenamiento redundante por zona.",
		"fr": "Vérifie les ressources ROS Alibaba Cloud pour le placement multi-zone, le basculement des équilibreurs, les nombres de réplicas de base et le stockage redondant par zone.",
		"pt": "Verifica recursos ROS da Alibaba Cloud quanto a posicionamento multi-zona, failover de balanceadores, contagens base de réplicas e armazenamento redundante por zona.",
	},
	"rules": [
		"rds-instance-zone-required",
		"rds-instance-secondary-zone-required",
		"polardb-cluster-multi-zone",
		"redis-instance-multi-zone",
		"mongodb-instance-multi-zone",
		"slb-instance-master-zone-required",
		"slb-instance-multi-zone",
		"alb-instance-multi-zone",
		"nlb-loadbalancer-multi-zone",
		"ess-scaling-group-multi-vswitch-distribution",
		"ecs-instance-group-min-amount-required",
		"ecs-instance-group-max-amount-required",
		"oss-zrs-enabled",
	],
}
