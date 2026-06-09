package infraguard.rules.aliyun.sls_project_multi_zone

import rego.v1

import data.infraguard.helpers

# Rule metadata
rule_meta := {
	"id": "sls-project-multi-zone",
	"severity": "medium",
	"name": {
		"en": "SLS Project Zone-Redundant Storage",
		"zh": "SLS 项目使用同城冗余存储",
		"ja": "SLS プロジェクトゾーン冗長ストレージ",
		"de": "SLS-Projekt Zonenredundanter Speicher",
		"es": "Almacenamiento Redundante de Zona del Proyecto SLS",
		"fr": "Stockage Redondant par Zone du Projet SLS",
		"pt": "Armazenamento Redundante de Zona do Projeto SLS"
	},
	"description": {
		"en": "SLS projects should use zone-redundant storage (ZRS) for high availability and data durability.",
		"zh": "SLS 项目应使用同城冗余存储（ZRS）以实现高可用性和数据持久性。",
		"ja": "SLS プロジェクトは、高可用性とデータの耐久性のためにゾーン冗長ストレージ（ZRS）を使用する必要があります。",
		"de": "SLS-Projekte sollten zonenredundanten Speicher (ZRS) für Hochverfügbarkeit und Datenbeständigkeit verwenden.",
		"es": "Los proyectos SLS deben usar almacenamiento redundante de zona (ZRS) para alta disponibilidad y durabilidad de datos.",
		"fr": "Les projets SLS doivent utiliser un stockage redondant par zone (ZRS) pour une haute disponibilité et une durabilité des données.",
		"pt": "Os projetos SLS devem usar armazenamento redundante de zona (ZRS) para alta disponibilidade e durabilidade dos dados."
	},
	"reason": {
		"en": "The SLS project does not use zone-redundant storage, which may affect data availability.",
		"zh": "SLS 项目未使用同城冗余存储，可能影响数据可用性。",
		"ja": "SLS プロジェクトがゾーン冗長ストレージを使用していないため、データの可用性に影響を与える可能性があります。",
		"de": "Das SLS-Projekt verwendet keinen zonenredundanten Speicher, was die Datenverfügbarkeit beeinträchtigen kann.",
		"es": "El proyecto SLS no usa almacenamiento redundante de zona, lo que puede afectar la disponibilidad de datos.",
		"fr": "Le projet SLS n'utilise pas de stockage redondant par zone, ce qui peut affecter la disponibilité des données.",
		"pt": "O projeto SLS não usa armazenamento redundante de zona, o que pode afetar a disponibilidade dos dados."
	},
	"recommendation": {
		"en": "Enable zone-redundant storage by setting DataRedundancyType to 'ZRS' when creating the project.",
		"zh": "在创建项目时通过将 DataRedundancyType 设置为'ZRS'来启用同城冗余存储。",
		"ja": "プロジェクト作成時に DataRedundancyType を 'ZRS' に設定してゾーン冗長ストレージを有効にします。",
		"de": "Aktivieren Sie zonenredundanten Speicher, indem Sie DataRedundancyType beim Erstellen des Projekts auf 'ZRS' setzen.",
		"es": "Habilite el almacenamiento redundante de zona estableciendo DataRedundancyType en 'ZRS' al crear el proyecto.",
		"fr": "Activez le stockage redondant par zone en définissant DataRedundancyType sur 'ZRS' lors de la création du projet.",
		"pt": "Habilite o armazenamento redundante de zona definindo DataRedundancyType como 'ZRS' ao criar o projeto."
	},
	"resource_types": ["ALIYUN::SLS::Project"]
}

# Check if project has ZRS enabled
has_zrs_enabled(resource) if {
	redundancy_type := helpers.get_property(resource, "DataRedundancyType", "LRS")
	redundancy_type == "ZRS"
}

# Deny rule: SLS projects should have ZRS enabled
deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::SLS::Project")
	not has_zrs_enabled(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "DataRedundancyType"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
