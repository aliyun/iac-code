package infraguard.rules.aliyun.actiontrail_trail_intact_enabled

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "actiontrail-trail-intact-enabled",
	"severity": "high",
	"name": {
		"en": "ActionTrail Trail Intact Enabled",
		"zh": "开启操作审计全量日志跟踪",
		"ja": "ActionTrail トレイルが完全に有効",
		"de": "ActionTrail Trail vollständig aktiviert",
		"es": "Trilha ActionTrail Intacta Habilitada",
		"fr": "Piste ActionTrail Intacte Activée",
		"pt": "Trilha ActionTrail Intacta Habilitada"
	},
	"description": {
		"en": "ActionTrail trail should be enabled and track all event types (Read and Write).",
		"zh": "操作审计中存在开启状态的跟踪，且跟踪全部地域和全部事件类型。",
		"ja": "ActionTrail トレイルを有効にし、すべてのイベントタイプ（読み取りと書き込み）を追跡する必要があります。",
		"de": "ActionTrail Trail sollte aktiviert sein und alle Ereignistypen (Lesen und Schreiben) verfolgen.",
		"es": "La trilha ActionTrail debe estar habilitada y rastrear todos los tipos de eventos (Lectura y Escritura).",
		"fr": "La piste ActionTrail doit être activée et suivre tous les types d'événements (Lecture et Écriture).",
		"pt": "A trilha ActionTrail deve estar habilitada e rastrear todos os tipos de eventos (Leitura e Escrita)."
	},
	"reason": {
		"en": "The ActionTrail trail is not enabled or does not track all event types.",
		"zh": "操作审计跟踪未开启或未跟踪所有事件类型。",
		"ja": "ActionTrail トレイルが有効になっていないか、すべてのイベントタイプを追跡していません。",
		"de": "Der ActionTrail Trail ist nicht aktiviert oder verfolgt nicht alle Ereignistypen.",
		"es": "La trilha ActionTrail no está habilitada o no rastrea todos los tipos de eventos.",
		"fr": "La piste ActionTrail n'est pas activée ou ne suit pas tous les types d'événements.",
		"pt": "A trilha ActionTrail não está habilitada ou não rastreia todos os tipos de eventos."
	},
	"recommendation": {
		"en": "Enable the trail using ALIYUN::ACTIONTRAIL::TrailLogging and set EventRW to All in ALIYUN::ACTIONTRAIL::Trail.",
		"zh": "使用 ALIYUN::ACTIONTRAIL::TrailLogging 启用跟踪，并在 ALIYUN::ACTIONTRAIL::Trail 中将 EventRW 设置为 All。",
		"ja": "ALIYUN::ACTIONTRAIL::TrailLogging を使用してトレイルを有効にし、ALIYUN::ACTIONTRAIL::Trail で EventRW を All に設定します。",
		"de": "Aktivieren Sie den Trail mit ALIYUN::ACTIONTRAIL::TrailLogging und setzen Sie EventRW in ALIYUN::ACTIONTRAIL::Trail auf All.",
		"es": "Habilite la trilha usando ALIYUN::ACTIONTRAIL::TrailLogging y establezca EventRW en All en ALIYUN::ACTIONTRAIL::Trail.",
		"fr": "Activez la piste en utilisant ALIYUN::ACTIONTRAIL::TrailLogging et définissez EventRW sur All dans ALIYUN::ACTIONTRAIL::Trail.",
		"pt": "Habilite a trilha usando ALIYUN::ACTIONTRAIL::TrailLogging e defina EventRW como All em ALIYUN::ACTIONTRAIL::Trail."
	},
	"resource_types": ["ALIYUN::ACTIONTRAIL::Trail"]
}

# Get all enabled trail names from TrailLogging resources
enabled_trails := {name |
	some logging in helpers.resources_by_type("ALIYUN::ACTIONTRAIL::TrailLogging")
	helpers.get_property(logging, "Enable", false) == true
	name := helpers.get_property(logging, "Name", "")
	name != ""
}

# Check if a trail is enabled (referenced by an enabled TrailLogging)
is_trail_enabled(trail_name) if {
	trail_name in enabled_trails
}

# Check if trail tracks all events
is_track_all_events(resource) if {
	helpers.get_property(resource, "EventRW", "Write") == "All"
}

deny contains result if {
	some name, resource in helpers.resources_by_types(rule_meta.resource_types)

	# Get the trail name (either property Name or resource name if not set?)
	# Trail Name property is required.
	trail_name := helpers.get_property(resource, "Name", "")

	# Check conditions
	violation := check_violation(trail_name, resource)
	violation != null

	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "EventRW"], # Approximate path
		"meta": {
			"severity": rule_meta.severity,
			"reason": violation,
			"recommendation": rule_meta.recommendation,
		},
	}
}

check_violation(trail_name, resource) := reason if {
	not is_track_all_events(resource)
	reason := rule_meta.reason.zh # "Not tracking all events"
}

check_violation(trail_name, resource) := reason if {
	is_track_all_events(resource)
	not is_trail_enabled(trail_name)
	reason := "操作审计跟踪未开启 (缺少启用的 TrailLogging)"
}
