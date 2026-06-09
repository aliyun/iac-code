package infraguard.packs.aliyun.iac_code_network_architecture

import rego.v1

pack_meta := {
    "id": "iac-code-network-architecture",
    "name": {
        "en": "IaC Code Network Architecture Pack",
        "zh": "IaC Code 网络架构合规包",
        "ja": "IaC Code ネットワークアーキテクチャパック",
        "de": "IaC Code Netzwerkarchitektur-Paket",
        "es": "Paquete de Arquitectura de Red de IaC Code",
        "fr": "Pack Architecture Réseau IaC Code",
        "pt": "Pacote de Arquitetura de Rede IaC Code"
    },
    "description": {
        "en": "InfraGuard policies for VPC address planning, zone placement, private network exposure, and enterprise network hub attachments.",
        "zh": "覆盖 VPC 地址规划、可用区落点、私网暴露控制和企业网络枢纽连接的 InfraGuard 策略组合。",
        "ja": "VPC アドレス計画、ゾーン配置、プライベートネットワーク公開、エンタープライズネットワークハブ接続のための InfraGuard ポリシーです。",
        "de": "InfraGuard-Richtlinien für VPC-Adressplanung, Zonenplatzierung, private Netzwerkexposition und Unternehmensnetzwerk-Hub-Anbindungen.",
        "es": "Políticas de InfraGuard para planificación de direcciones VPC, ubicación por zonas, exposición de red privada y conexiones a hubs de red empresariales.",
        "fr": "Politiques InfraGuard pour la planification d'adresses VPC, le placement par zone, l'exposition réseau privée et les connexions aux hubs réseau d'entreprise.",
        "pt": "Políticas InfraGuard para planejamento de endereços VPC, posicionamento por zona, exposição de rede privada e conexões a hubs de rede empresariais."
    },
    "rules": [
        "vpc-cidr-required",
        "vswitch-cidr-required",
        "vswitch-zone-required",
        "security-group-vpc-required",
        "security-group-enterprise-type",
        "nat-gateway-vpc-required",
        "eip-explicit-bandwidth-required",
        "slb-address-type-intranet",
        "alb-address-type-intranet",
        "nlb-address-type-intranet",
        "vpn-gateway-vpc-required",
        "cen-instance-name-required",
        "transit-router-vpc-attachment-multi-zone"
    ]
}
