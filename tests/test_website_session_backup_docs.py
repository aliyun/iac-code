from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
WEBSITE_ROOT = PROJECT_ROOT / "website"

LOCALE_DOC_ROOTS = [
    WEBSITE_ROOT / "docs",
    WEBSITE_ROOT / "i18n" / "zh-Hans" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "ja" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "fr" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "de" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "es" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "pt" / "docusaurus-plugin-content-docs" / "current",
]


def test_website_documents_session_backup_in_all_locales() -> None:
    missing: list[str] = []
    checks = {
        "configuration/environment-variables.md": [
            "IAC_CODE_CONFIG_BACKUP_DIR",
            "<backup>/projects/<project>/<session_id>/",
            "normal_turn_end",
            "critical",
            "%VAR%",
            "$env:VAR",
            "UNC",
            ".backup-lock",
            "reparse",
        ],
        "configuration/runtime-configuration.md": [
            "<config-dir>/projects/<project>/<session-id>/",
            "<config-dir>/projects/<project>/<session-id>/metadata.json",
            "<config-dir>/projects/<project>/<session-id>/session.jsonl",
            "<config-dir>/projects/<project>/<session-id>/usage.jsonl",
            "<config-dir>/projects/<project>/<session-id>/permission-audit.jsonl",
            "<config-dir>/projects/<project>/<session-id>/a2a/task.json",
            "<config-dir>/projects/<project>/<session-id>/a2a/context.json",
            "<config-dir>/projects/<project>/<session-id>/a2a/artifacts/",
            "<config-dir>/projects/<project>/<session-id>/a2a/pipeline/",
            "<config-dir>/projects/<project>/<session-id>/a2a/cleanup-deferred-prompts.json",
            "<config-dir>/projects/<project>/<session-id>/pipeline/meta.yaml",
            "<config-dir>/projects/<project>/<session-id>/pipeline/context.yaml",
            "<config-dir>/projects/<project>/<session-id>/pipeline/events.jsonl",
            "<config-dir>/projects/<project>/<session-id>/pipeline/display.jsonl",
            "<config-dir>/projects/<project>/<session-id>/pipeline/transcripts/<transcript-id>/",
            "<config-dir>/projects/<project>/<session-id>/pipeline/transcripts/<transcript-id>/session.jsonl",
            "<config-dir>/projects/<project>/<session-id>/pipeline/transcripts/<transcript-id>/usage.jsonl",
            "<config-dir>/projects/<project>/<session-id>/pipeline/transcripts/<transcript-id>/permission-audit.jsonl",
            "<config-dir>/projects/<project>/<session-id>/pipeline/transcripts/<transcript-id>/tool-results/",
            "layout_version",
            ".backup-state.json",
            "image-cache/",
            "tool-results/",
        ],
        "a2a/protocol-reference.md": [
            "backup_blocked",
            "backupBlocked",
            "metadata.iac_code.pipeline.eventType",
            "backup_committed",
            "committedEventId",
            "committedEventType",
            "committedSequence",
            "TASK_STATE_INPUT_REQUIRED",
            "normal_turn_end",
            "pipeline_step_completed",
            "input_required",
            "pipeline_handoff_ready",
            "waiting_input",
            "terminal",
            "handoff_ready",
        ],
        "automation/non-interactive-mode.md": [
            "IAC_CODE_CONFIG_BACKUP_DIR",
            "normal_turn_end",
            "warning",
            ".backup-state.json",
        ],
        "acp/protocol-reference.md": [
            "IAC_CODE_CONFIG_BACKUP_DIR",
            "normal_turn_end",
            "warning",
            ".backup-state.json",
        ],
        "automation/pipeline-mode.md": [
            "backup_blocked",
            "backup_committed",
            "committedEventId",
            "committedEventType",
            "committedSequence",
            "input_required",
            "waiting_input",
            "terminal",
            "pipeline_handoff_ready",
            "parallel_sub_pipeline",
        ],
        "a2a/overview.md": [
            "IAC_CODE_CONFIG_BACKUP_DIR",
            "<config>/a2a/tasks",
            "<config>/a2a/contexts",
        ],
        "mcp/capabilities.md": [
            "tool-results/mcp",
            "<config-dir>/projects/<project>/<session-id>/tool-results",
            "<config-dir>/tool-results/<session-id>",
        ],
        "mcp/troubleshooting.md": [
            "tool-results/mcp",
            "<config-dir>/projects/<project>/<session-id>/tool-results",
            "<config-dir>/tool-results/<session-id>",
        ],
    }

    for root in LOCALE_DOC_ROOTS:
        for relative_path, needles in checks.items():
            path = root / relative_path
            if not path.exists():
                missing.append(f"{path.relative_to(PROJECT_ROOT)}: missing file")
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle not in text:
                    missing.append(f"{path.relative_to(PROJECT_ROOT)}: missing {needle!r}")

    assert not missing, "\n".join(missing)


def test_session_backup_docs_do_not_promise_warning_response_fields() -> None:
    forbidden = {
        "automation/non-interactive-mode.md": [
            "return a `warning`",
            "returns a `warning`",
            "devuelve un `warning`",
            "renvoie un `warning`",
            "retorna um `warning`",
            "liefert ein `warning`",
            "`warning` を返し",
            "返回 `warning`",
        ],
        "acp/protocol-reference.md": [
            "complete with a `warning`",
            "completes with a `warning`",
            "completarse con un `warning`",
            "terminer avec un `warning`",
            "concluir com um `warning`",
            "mit einem `warning` abgeschlossen",
            "`warning` 付きで完了",
            "带 `warning` 完成",
        ],
    }
    violations: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        for relative_path, phrases in forbidden.items():
            path = root / relative_path
            text = path.read_text(encoding="utf-8")
            for phrase in phrases:
                if phrase in text:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: forbidden {phrase!r}")

    assert not violations, "\n".join(violations)


def test_session_backup_docs_do_not_use_legacy_audit_or_lock_locations() -> None:
    forbidden = [
        "<config-dir>/logs/permission-audit.jsonl",
        "logs/permission-audit.jsonl",
        "directory locks",
        "目录锁",
        "ディレクトリロック",
        "verrous de répertoire",
        "bloqueos de directorio",
        "locks de diretório",
        "权限审计记录仍位于 `<config-dir>/logs/`",
        "権限監査レコードは `<config-dir>/logs/` に残ります",
    ]
    violations: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        for path in root.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for phrase in forbidden:
                if phrase in text:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: forbidden {phrase!r}")

    assert not violations, "\n".join(violations)


def test_backup_committed_docs_scope_terminal_and_handoff_publications() -> None:
    required_protocol_tokens = [
        "`pipeline_step_completed`",
        "`input_required`",
        "`waiting_input`",
        "`terminal`",
        "`handoff_ready`",
        "`pipeline_handoff_ready`",
        "`backup_committed`",
        "`committedEventId`",
        "`committedEventType`",
        "`committedSequence`",
    ]
    required_pipeline_tokens = [
        "`backup_blocked`",
        "`backup_committed`",
        "`committedEventId`",
        "`committedEventType`",
        "`committedSequence`",
        "`pipeline_handoff_ready`",
        "terminal",
    ]
    missing: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        protocol = (root / "a2a" / "protocol-reference.md").read_text(encoding="utf-8")
        pipeline_mode = (root / "automation" / "pipeline-mode.md").read_text(encoding="utf-8")
        for token in required_protocol_tokens:
            if token not in protocol:
                path = (root / "a2a" / "protocol-reference.md").relative_to(PROJECT_ROOT)
                missing.append(f"{path}: missing {token}")
        for token in required_pipeline_tokens:
            if token not in pipeline_mode:
                path = (root / "automation" / "pipeline-mode.md").relative_to(PROJECT_ROOT)
                missing.append(f"{path}: missing {token}")

    assert not missing, "\n".join(missing)
