package infraguard.packs.aliyun.iac_code_cost_optimization

import rego.v1

pack_meta := {
	"id": "iac-code-cost-optimization",
	"name": {
		"en": "IaC Code Cost Optimization Pack",
		"zh": "IaC Code 成本优化合规包",
		"ja": "IaC Code コスト最適化パック",
		"de": "IaC Code Kostenoptimierungspaket",
		"es": "Paquete de optimización de costos de IaC Code",
		"fr": "Pack d'optimisation des coûts IaC Code",
		"pt": "Pacote de otimização de custos do IaC Code"
	},
	"description": {
		"en": "InfraGuard policies that make cost-bearing ROS choices explicit for compute, storage, database, cache, network, and log resources.",
		"zh": "用于显式检查 ROS 中计算、存储、数据库、缓存、网络和日志资源成本相关配置的 InfraGuard 策略组合。",
		"ja": "コンピュート、ストレージ、データベース、キャッシュ、ネットワーク、ログリソースのコストに関わる ROS 選択を明示させる InfraGuard ポリシーです。",
		"de": "InfraGuard-Richtlinien, die kostenrelevante ROS-Entscheidungen für Compute-, Speicher-, Datenbank-, Cache-, Netzwerk- und Logressourcen explizit machen.",
		"es": "Políticas de InfraGuard que hacen explícitas las decisiones ROS con impacto de costo para recursos de cómputo, almacenamiento, base de datos, caché, red y logs.",
		"fr": "Politiques InfraGuard qui rendent explicites les choix ROS ayant un impact de coût pour les ressources de calcul, stockage, base de données, cache, réseau et journaux.",
		"pt": "Políticas InfraGuard que tornam explícitas as escolhas ROS com impacto de custo para recursos de computação, armazenamento, banco de dados, cache, rede e logs."
	},
	"rules": [
		"ecs-instance-charge-type-required",
		"ecs-instance-bandwidth-configured",
		"ecs-instance-type-required",
		"ecs-disk-category-required",
		"ecs-disk-size-required",
		"rds-pay-type-required",
		"rds-storage-type-required",
		"redis-instance-class-required",
		"slb-internet-charge-type-required",
		"eip-bandwidth-required",
		"nat-gateway-spec-required",
		"oss-storage-class-required",
		"logstore-ttl-required"
	]
}
