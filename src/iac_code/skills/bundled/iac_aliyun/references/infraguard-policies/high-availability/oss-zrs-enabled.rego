package infraguard.rules.aliyun.oss_zrs_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "oss-zrs-enabled",
	"severity": "medium",
	"name": {
		"en": "OSS Bucket Zone-Redundant Storage Enabled",
		"zh": "OSS Bucket 启用同城冗余存储",
		"ja": "OSS バケットのゾーン冗長ストレージ有効化",
		"de": "OSS-Bucket mit zonenredundantem Speicher",
		"es": "Almacenamiento con redundancia de zona para bucket OSS",
		"fr": "Stockage redondant par zone pour bucket OSS",
		"pt": "Armazenamento com redundância de zona para bucket OSS",
	},
	"description": {
		"en": "OSS buckets should use zone-redundant storage to keep data available when one zone becomes unavailable.",
		"zh": "OSS Bucket 应使用同城冗余存储，以便在单个可用区不可用时仍可访问数据。",
		"ja": "OSS バケットは、1 つのゾーンが利用できなくなってもデータにアクセスできるように、ゾーン冗長ストレージを使用する必要があります。",
		"de": "OSS-Buckets sollten zonenredundanten Speicher verwenden, damit Daten verfügbar bleiben, wenn eine Zone nicht verfügbar ist.",
		"es": "Los buckets OSS deben usar almacenamiento con redundancia de zona para mantener los datos disponibles si una zona deja de estar disponible.",
		"fr": "Les buckets OSS doivent utiliser le stockage redondant par zone afin que les données restent disponibles lorsqu'une zone devient indisponible.",
		"pt": "Buckets OSS devem usar armazenamento com redundância de zona para manter os dados disponíveis quando uma zona ficar indisponível.",
	},
	"reason": {
		"en": "The OSS bucket does not use ZRS, so data availability depends on locally redundant storage.",
		"zh": "OSS Bucket 未使用 ZRS，数据可用性依赖本地冗余存储。",
		"ja": "OSS バケットが ZRS を使用していないため、データ可用性はローカル冗長ストレージに依存します。",
		"de": "Der OSS-Bucket verwendet kein ZRS, daher hängt die Datenverfügbarkeit von lokal redundantem Speicher ab.",
		"es": "El bucket OSS no usa ZRS, por lo que la disponibilidad de datos depende del almacenamiento redundante local.",
		"fr": "Le bucket OSS n'utilise pas ZRS, la disponibilité des données dépend donc du stockage redondant local.",
		"pt": "O bucket OSS não usa ZRS, portanto a disponibilidade dos dados depende de armazenamento redundante local.",
	},
	"recommendation": {
		"en": "Set RedundancyType to ZRS when creating the bucket.",
		"zh": "创建 Bucket 时将 RedundancyType 设置为 ZRS。",
		"ja": "バケット作成時に RedundancyType を ZRS に設定します。",
		"de": "Setzen Sie RedundancyType beim Erstellen des Buckets auf ZRS.",
		"es": "Establezca RedundancyType en ZRS al crear el bucket.",
		"fr": "Définissez RedundancyType sur ZRS lors de la création du bucket.",
		"pt": "Defina RedundancyType como ZRS ao criar o bucket.",
	},
	"resource_types": ["ALIYUN::OSS::Bucket"],
}

has_zrs_enabled(resource) if {
	redundancy_type := helpers.get_property(resource, "RedundancyType", "LRS")
	redundancy_type == "ZRS"
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
	not has_zrs_enabled(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "RedundancyType"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}
