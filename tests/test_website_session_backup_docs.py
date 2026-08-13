from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from iac_code.cli.main import app as iac_code_app

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
            "IAC_CODE_CONFIG_BACKUP_TMP_DIR",
            "<session_id>_vX",
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
            "IAC_CODE_CONFIG_BACKUP_TMP_DIR",
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
            "IAC_CODE_CONFIG_BACKUP_TMP_DIR",
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


def test_mcp_cli_docs_make_health_checks_explicit() -> None:
    config_text = (WEBSITE_ROOT / "docs" / "mcp" / "configuration.md").read_text(encoding="utf-8")
    troubleshooting_text = (WEBSITE_ROOT / "docs" / "mcp" / "troubleshooting.md").read_text(encoding="utf-8")

    assert (
        "`iac-code mcp list` | List configured servers, scopes, transports, and approval status without connecting."
    ) in config_text
    assert ("`iac-code mcp list --config-only` | Alias for the default config listing.") in config_text
    assert "`iac-code mcp list --check` | Connect briefly and show bounded health diagnostics." in config_text
    assert "`iac-code mcp get` | Print one redacted server config without connecting." in config_text
    assert "`iac-code mcp get --config-only` | Print one redacted server config without connecting." in config_text
    assert (
        "`iac-code mcp get --check` | Connect briefly and show bounded health diagnostics for one server."
        in config_text
    )
    assert "Inspect configured servers without connecting:" in troubleshooting_text
    assert "iac-code mcp list\n" in troubleshooting_text
    assert "Run bounded health diagnostics for configured servers:" in troubleshooting_text
    assert "iac-code mcp list --check" in troubleshooting_text
    assert "iac-code mcp list --config-only" in troubleshooting_text
    assert "iac-code mcp get my-server --scope local --check" in troubleshooting_text

    failures: list[str] = []
    for root in LOCALE_DOC_ROOTS:
        text = (root / "mcp" / "troubleshooting.md").read_text(encoding="utf-8")
        if "iac-code mcp list --config-only" not in text:
            failures.append(f"{root.relative_to(PROJECT_ROOT)}: missing mcp list --config-only")
        if "iac-code mcp get my-server --scope local --check" not in text:
            failures.append(f"{root.relative_to(PROJECT_ROOT)}: missing mcp get --check")
        if "列出已配置 servers" in text:
            failures.append(f"{root.relative_to(PROJECT_ROOT)}: still describes mcp list as config-only")

    assert not failures, "\n".join(failures)


def test_mcp_oauth_and_troubleshooting_docs_describe_cleanup_and_auth_failures(tmp_path: Path) -> None:
    not_found_result = CliRunner().invoke(
        iac_code_app,
        ["mcp", "get", "missing-server", "--scope", "user", "--config-only"],
        env={"IAC_CODE_CONFIG_DIR": str(tmp_path), "NO_COLOR": "1"},
    )
    assert not_found_result.exit_code == 1
    runtime_not_found_message = "MCP server 'missing-server' not found in user config."
    assert runtime_not_found_message in not_found_result.output

    required_oauth_tokens = [
        "iac-code mcp reset-auth secure-reviewer --scope user",
        "iac-code mcp remove secure-reviewer --scope user",
        "OAuth token state",
        "dynamic client registration state",
        "`client_id`",
        "`client_secret`",
        "signature index",
        "`MCPSecretStorage`",
    ]
    required_troubleshooting_tokens = [
        "MCP server 'name' not found in persisted MCP config.",
        "MCP server 'name' not found in user config.",
        "not found in user config",
        "iac-code mcp list --config-only",
        "iac-code mcp get name --scope user --config-only",
        "iac-code mcp get name --scope user --source-path",
        "auth-failed",
        "MCP auth failed for 'name':",
        "callback URL",
        "authorization code",
        "iac-code mcp reset-auth name --scope user",
        "pending_approval",
        "needs-auth",
        "invalid_client",
        "insufficient_scope",
        "connection_failed",
        "scope ambiguity",
    ]

    failures: list[str] = []
    for root in LOCALE_DOC_ROOTS:
        locale = "en" if root == WEBSITE_ROOT / "docs" else _locale_for_doc_root(root)
        oauth = (root / "mcp" / "oauth-and-security.md").read_text(encoding="utf-8")
        troubleshooting = (root / "mcp" / "troubleshooting.md").read_text(encoding="utf-8")
        for token in required_oauth_tokens:
            if token not in oauth:
                failures.append(f"{locale} oauth-and-security.md: missing {token!r}")
        for token in required_troubleshooting_tokens:
            if token not in troubleshooting:
                failures.append(f"{locale} troubleshooting.md: missing {token!r}")

    assert not failures, "\n".join(failures)


def test_mcp_configuration_docs_start_with_claude_style_quick_start() -> None:
    docs = list(
        dict.fromkeys(
            [WEBSITE_ROOT / "docs" / "mcp" / "configuration.md"]
            + [root / "mcp" / "configuration.md" for root in LOCALE_DOC_ROOTS]
        )
    )
    remote_add = "iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp"
    remote_auth = "iac-code mcp auth yuque"
    stdio_add = "iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp"
    legacy_add = "iac-code mcp add local-catalog"
    failures: list[str] = []

    for path in docs:
        text = path.read_text(encoding="utf-8")
        if path == WEBSITE_ROOT / "docs" / "mcp" / "configuration.md" and "## Quick Start" not in text:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing Quick Start section")
        positions = {
            label: text.find(value)
            for label, value in {
                "remote add": remote_add,
                "remote auth": remote_auth,
                "stdio add": stdio_add,
                "legacy add": legacy_add,
            }.items()
        }
        for label, position in positions.items():
            if position < 0:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing {label} example")
        if any(position < 0 for position in positions.values()):
            continue
        first_legacy = positions["legacy add"]
        for label in ("remote add", "remote auth", "stdio add"):
            if positions[label] > first_legacy:
                failures.append(
                    f"{path.relative_to(PROJECT_ROOT)}: {label} example appears after legacy local add example"
                )

    assert not failures, "\n".join(failures)


def test_mcp_docs_have_dedicated_quick_start_entry() -> None:
    remote_add = "iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp"
    remote_auth = "iac-code mcp auth yuque"
    stdio_add = "iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp"
    quick_start_relative = "mcp/quick-start.md"
    failures: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        quick_start = root / quick_start_relative
        if not quick_start.exists():
            failures.append(f"{quick_start.relative_to(PROJECT_ROOT)}: missing file")
            continue
        text = quick_start.read_text(encoding="utf-8")
        for label, command in {
            "remote HTTP add": remote_add,
            "OAuth auth": remote_auth,
            "stdio passthrough add": stdio_add,
        }.items():
            if command not in text:
                failures.append(f"{quick_start.relative_to(PROJECT_ROOT)}: missing {label} command")
        if "iac-code mcp add local-catalog" in text:
            failures.append(
                f"{quick_start.relative_to(PROJECT_ROOT)}: quick start should not lead with legacy local add"
            )

        overview = (root / "mcp" / "overview.md").read_text(encoding="utf-8")
        if "./quick-start.md" not in overview:
            failures.append(f"{(root / 'mcp' / 'overview.md').relative_to(PROJECT_ROOT)}: missing quick-start link")

    sidebar = (WEBSITE_ROOT / "sidebars.ts").read_text(encoding="utf-8")
    sidebar_items = [
        "'mcp/overview'",
        "'mcp/quick-start'",
        "'mcp/configuration'",
    ]
    positions = {item: sidebar.find(item) for item in sidebar_items}
    for item, position in positions.items():
        if position < 0:
            failures.append(f"website/sidebars.ts: missing {item}")
    if all(position >= 0 for position in positions.values()) and not (
        positions["'mcp/overview'"] < positions["'mcp/quick-start'"] < positions["'mcp/configuration'"]
    ):
        failures.append("website/sidebars.ts: MCP quick start is not between overview and configuration")

    assert not failures, "\n".join(failures)


def test_mcp_docs_relative_markdown_links_resolve() -> None:
    failures: list[str] = []
    link_pattern = re.compile(r"\[[^\]]+\]\((\./[^)#?]+\.md)(?:#[^)]+)?\)")

    for root in LOCALE_DOC_ROOTS:
        for path in (root / "mcp").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            for match in link_pattern.finditer(text):
                target = (path.parent / match.group(1)).resolve()
                if not target.exists():
                    failures.append(f"{path.relative_to(PROJECT_ROOT)}: link {match.group(1)!r} targets missing file")

    assert not failures, "\n".join(failures)


def test_mcp_configuration_docs_list_real_command_options() -> None:
    commands = [
        "add",
        "add-json",
        "list",
        "get",
        "remove",
        "approve",
        "reject",
        "reset-project-choices",
        "auth",
        "reset-auth",
        "reconnect",
        "disable",
        "enable",
    ]
    expected_options = {command: _mcp_help_options(command) for command in commands}
    failures: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        path = root / "mcp" / "configuration.md"
        rows = _markdown_table_rows(path.read_text(encoding="utf-8"))
        for command, options in expected_options.items():
            command_literal = f"`iac-code mcp {command}`"
            matching_rows = [row for row in rows if row and row[0] == command_literal]
            if not matching_rows:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing option row for {command_literal}")
                continue
            if options and not any(all(option in " ".join(row[1:]) for option in options) for row in matching_rows):
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: {command_literal} row missing option(s) {options}")
            if not options and not any("No command-specific options" in " ".join(row[1:]) for row in matching_rows):
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: {command_literal} row must state no extra options")

    assert not failures, "\n".join(failures)


def test_cli_command_docs_include_interactive_mcp_command() -> None:
    failures: list[str] = []
    docs = [WEBSITE_ROOT / "docs" / "cli" / "commands.md"]
    docs.extend(root / "cli" / "commands.md" for root in LOCALE_DOC_ROOTS)

    for path in docs:
        text = path.read_text(encoding="utf-8")
        if "| `/mcp" not in text:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing /mcp command row")
        if "Manage MCP servers" not in text and "MCP" not in text:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing MCP description")

    assert not failures, "\n".join(failures)


def test_acp_docs_describe_mcp_servers_as_functional_session_config() -> None:
    forbidden = [
        "accepted but not yet functional",
        "已接受但尚未生效",
        "noch nicht funktional",
        "aun no funcional",
        "pas encore fonctionnelle",
        "まだ機能しません",
        "ainda nao funcional",
    ]
    failures: list[str] = []
    docs = list(
        dict.fromkeys(
            [WEBSITE_ROOT / "docs" / "acp" / "protocol-reference.md"]
            + [root / "acp" / "protocol-reference.md" for root in LOCALE_DOC_ROOTS]
        )
    )

    for path in docs:
        text = path.read_text(encoding="utf-8")
        if "`mcpServers`" not in text:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing mcpServers")
        for phrase in forbidden:
            if phrase in text:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: stale phrase {phrase!r}")

    assert not failures, "\n".join(failures)


def test_acp_docs_describe_mcp_servers_as_array_config() -> None:
    failures: list[str] = []
    docs = [WEBSITE_ROOT / "docs" / "acp" / "protocol-reference.md"]
    docs.extend(root / "acp" / "protocol-reference.md" for root in LOCALE_DOC_ROOTS)

    for path in docs:
        text = path.read_text(encoding="utf-8")
        mcp_lines = [line for line in text.splitlines() if "`mcpServers`" in line]
        if not mcp_lines:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing mcpServers row")
            continue
        row = mcp_lines[0]
        if "| object |" in row:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: mcpServers is documented as object")
        if "array" not in row and "MCPServer[]" not in row:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: mcpServers type does not describe an array")

    assert not failures, "\n".join(failures)


def test_mcp_configuration_docs_use_acp_mcpservers_wire_name() -> None:
    failures: list[str] = []
    docs = list(
        dict.fromkeys(
            [WEBSITE_ROOT / "docs" / "mcp" / "configuration.md"]
            + [root / "mcp" / "configuration.md" for root in LOCALE_DOC_ROOTS]
        )
    )

    for path in docs:
        text = path.read_text(encoding="utf-8")
        if "mcp_servers" in text:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: uses stale mcp_servers wire name")
        acp_session_rows = [line for line in text.splitlines() if "ACP" in line and "`session`" in line]
        if not acp_session_rows:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing ACP session config row")
            continue
        if not any("`mcpServers`" in line for line in acp_session_rows):
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: ACP session row missing mcpServers")

    assert not failures, "\n".join(failures)


def test_acp_docs_describe_mcp_status_warning_and_permission_metadata() -> None:
    required_literals = [
        "`mcpStatus`",
        "`mcpWarning`",
        "`_meta`",
        "`session/update.params.update._meta.iac_code`",
        "`session_info_update`",
        "`agent_message_chunk`",
        "`servers`",
        "`warnings`",
        "`serverName`",
        "`state`",
        "`authState`",
        "`configured`",
        "`not-configured`",
        "`toolsCount`",
        "`resourcesCount`",
        "`promptsCount`",
        "`truncated`",
        "`truncationReason`",
        "`acp-frame-size-limit`",
        "`serversOmittedCount`",
        "`warningsOmittedCount`",
        "`connected`",
        "`failed`",
        "`pending`",
        "`needs-auth`",
        "`pending-approval`",
        "`disabled`",
        "`duplicate_config`",
        "`invalid_name`",
        "`invalid_config`",
        "`missing_env`",
        "`pending_approval`",
        "`needs_auth`",
        "`connection_failed`",
        "`command_conflict`",
        "`skill_read_failed`",
        "`skill_truncated`",
        "`alias_conflict`",
        "`<capability>_failed`",
        "`severity`",
        "`source`",
        "`sourcePath`",
        "`publicName`",
        "`originalServerName`",
        "`originalToolName`",
        "`annotations`",
        "`readOnlyHint`",
        "`destructiveHint`",
        "`ToolCallUpdate`",
        "`title`",
        "`toolCall._meta.iac_code.permission`",
        "`permissionId`",
        "`toolName`",
        "`toolUseId`",
        "`scope`",
        "`inputSummary`",
        "`isReadOnly`",
        "`isDestructive`",
    ]
    failures: list[str] = []
    docs = list(
        dict.fromkeys(
            [WEBSITE_ROOT / "docs" / "acp" / "protocol-reference.md"]
            + [root / "acp" / "protocol-reference.md" for root in LOCALE_DOC_ROOTS]
        )
    )

    for path in docs:
        text = path.read_text(encoding="utf-8")
        for literal in required_literals:
            if literal not in text:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing {literal}")
        request_heading = re.search(r"(?m)^### .*request_permission.*$", text)
        if request_heading is None:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing request_permission section")
            continue
        request_section = text[request_heading.start() :]
        next_heading = request_section.find("\n### ", 1)
        if next_heading != -1:
            request_section = request_section[:next_heading]
        for literal in (
            "`toolCall._meta.iac_code.permission`",
            "`permissionId`",
            "`toolName`",
            "`toolUseId`",
            "`scope`",
            "`inputSummary`",
            "`publicName`",
            "`originalServerName`",
            "`originalToolName`",
            "`isReadOnly`",
            "`isDestructive`",
        ):
            if literal not in request_section:
                failures.append(
                    f"{path.relative_to(PROJECT_ROOT)}: request_permission section missing {literal} wire metadata"
                )
        if "not sent as additional `_meta` fields on `request_permission`" in request_section:
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: request_permission section denies MCP metadata")

    assert not failures, "\n".join(failures)


def test_locale_mcp_docs_are_not_english_copies() -> None:
    failures: list[str] = []
    english_root = WEBSITE_ROOT / "docs"
    for root in [item for item in LOCALE_DOC_ROOTS if item != english_root]:
        for english_path in (english_root / "mcp").glob("*.md"):
            localized = root / "mcp" / english_path.name
            if localized.read_text(encoding="utf-8") == english_path.read_text(encoding="utf-8"):
                failures.append(f"{localized.relative_to(PROJECT_ROOT)}: identical to English source")

    assert not failures, "\n".join(failures)


def test_locale_mcp_docs_do_not_keep_english_source_sentences() -> None:
    failures: list[str] = []
    english_root = WEBSITE_ROOT / "docs"
    for root in [item for item in LOCALE_DOC_ROOTS if item != english_root]:
        for english_path in (english_root / "mcp").glob("*.md"):
            localized = root / "mcp" / english_path.name
            english_lines = _english_prose_lines(english_path.read_text(encoding="utf-8"))
            localized_lines = set(_visible_markdown_lines(localized.read_text(encoding="utf-8")))
            for line in sorted(english_lines & localized_lines):
                failures.append(f"{localized.relative_to(PROJECT_ROOT)}: retained English source line: {line}")

    assert not failures, "\n".join(failures)


def test_locale_mcp_docs_preserve_important_inline_code_literals() -> None:
    failures: list[str] = []
    english_root = WEBSITE_ROOT / "docs"
    for english_path in (english_root / "mcp").glob("*.md"):
        required = _important_inline_code_literal_counts(english_path.read_text(encoding="utf-8"))
        for root in [item for item in LOCALE_DOC_ROOTS if item != english_root]:
            localized = root / "mcp" / english_path.name
            actual = _inline_code_literal_counts(localized.read_text(encoding="utf-8"))
            for literal, count in required.items():
                if actual[literal] < count:
                    failures.append(
                        "{}: expected at least {} occurrence(s) of `{}`, found {}".format(
                            localized.relative_to(PROJECT_ROOT),
                            count,
                            literal,
                            actual[literal],
                        )
                    )

    assert not failures, "\n".join(failures)


def test_locale_mcp_configuration_docs_preserve_config_key_literals_in_prose() -> None:
    failures: list[str] = []
    for root in LOCALE_DOC_ROOTS:
        path = root / "mcp" / "configuration.md"
        lines = _visible_markdown_lines(path.read_text(encoding="utf-8"))
        if not any("`--scope`" in line and "`local`" in line and "`user`" in line for line in lines):
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: scope fallback line must keep `local` and `user`")
        if not any(
            "`type`" in line and "`command`" in line and "`env`" in line and "`cmd /c npx`" in line and "`npx`" in line
            for line in lines
        ):
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: stdio prose must keep config key literals")
        if not any("`type`" in line and "`url`" in line for line in lines):
            failures.append(f"{path.relative_to(PROJECT_ROOT)}: remote server prose must keep `type` and `url`")

    assert not failures, "\n".join(failures)


def test_locale_mcp_troubleshooting_docs_use_localized_auth_action_labels() -> None:
    failures: list[str] = []
    english_root = WEBSITE_ROOT / "docs"
    for root in [item for item in LOCALE_DOC_ROOTS if item != english_root]:
        locale = _locale_for_doc_root(root)
        path = root / "mcp" / "troubleshooting.md"
        text = path.read_text(encoding="utf-8")
        auth_label = _message_translation(locale, "Authenticate")
        reauth_label = _message_translation(locale, "Re-authenticate")
        for label in [auth_label, reauth_label]:
            if f"`{label}`" not in text:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing localized action label `{label}`")
        for english_label in ["Authenticate", "Re-authenticate"]:
            if f"`{english_label}`" in text:
                failures.append(f"{path.relative_to(PROJECT_ROOT)}: kept English action label `{english_label}`")

    assert not failures, "\n".join(failures)


def _english_prose_lines(text: str) -> set[str]:
    lines: set[str] = set()
    for line in _visible_markdown_lines(text):
        if len(line) < 40:
            continue
        prose = re.sub(r"`[^`]+`", "", line)
        ascii_letters = [char for char in prose if char.isascii() and char.isalpha()]
        if len(ascii_letters) < 12:
            continue
        lines.add(line)
    return lines


def _important_inline_code_literal_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for literal, count in _inline_code_literal_counts(text).items():
        if _is_important_mcp_inline_code_literal(literal):
            counts[literal] = count
    return counts


def _inline_code_literal_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in _visible_markdown_lines(text):
        for literal in re.findall(r"`([^`\n]+)`", line):
            counts[literal] += 1
    return counts


def _is_important_mcp_inline_code_literal(literal: str) -> bool:
    if literal in {"Authenticate", "Re-authenticate"}:
        return False
    exact = {
        "args",
        "command",
        "env",
        "headers",
        "http",
        "local",
        "N",
        "oauth",
        "permissions",
        "project",
        "server",
        "session",
        "sse",
        "stdio",
        "type",
        "url",
        "uri",
        "user",
        "ws",
    }
    prefixes = ("iac", "list_", "mcp", "prompts", "read_", "resources", "skill")
    return bool(literal in exact or literal.startswith(prefixes) or re.search(r"[/:._$<>{}=\-]|[A-Z]", literal))


def _locale_for_doc_root(root: Path) -> str:
    parts = root.parts
    index = parts.index("i18n")
    locale = parts[index + 1]
    return "zh" if locale == "zh-Hans" else locale


def _message_translation(locale: str, msgid: str) -> str:
    path = PROJECT_ROOT / "src" / "iac_code" / "i18n" / "locales" / locale / "LC_MESSAGES" / "messages.po"
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("msgid "):
            current = _po_quoted_value(line)
        elif line.startswith("msgstr ") and current == msgid:
            return _po_quoted_value(line) or msgid
    return msgid


def _po_quoted_value(line: str) -> str:
    value = line.split(" ", 1)[1].strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _visible_markdown_lines(text: str) -> list[str]:
    lines: list[str] = []
    in_frontmatter = False
    in_code_fence = False
    for index, raw_line in enumerate(text.splitlines()):
        stripped = raw_line.strip()
        if index == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence or not stripped:
            continue
        if stripped.startswith("import "):
            continue
        lines.append(stripped)
    return lines


def _markdown_table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in _visible_markdown_lines(text):
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":"}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _mcp_help_options(command: str) -> list[str]:
    result = CliRunner().invoke(
        iac_code_app,
        ["mcp", command, "--help"],
        env={"COLUMNS": "200", "NO_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    return sorted({option for option in re.findall(r"--[a-z][a-z0-9-]*", result.output) if option != "--help"})


def test_mcp_docs_describe_enhanced_host_features_in_all_locales() -> None:
    required_by_page = {
        "mcp/configuration.md": [
            "`stdio`",
            "`http`",
            "`sse`",
            "`ws`",
            "`headersHelper`",
            "${VAR}",
            "`iac-code mcp reconnect`",
            "`iac-code mcp disable`",
            "`iac-code mcp enable`",
        ],
        "mcp/overview.md": [
            "`ws://`",
            "`wss://`",
            "`headersHelper`",
            "`http`",
            "`sse`",
        ],
        "mcp/oauth-and-security.md": [
            "`headersHelper`",
            "`clientSecretEnv`",
            "iac-code mcp reset-auth",
        ],
        "mcp/capabilities.md": [
            "`.txt`",
            "`.json`",
            "`.md`",
            "$<server>:<skill>",
        ],
    }
    forbidden = [
        "headersHelper` se rechazan",
        "`headersHelper` sont rejet",
        "`headersHelper`-Commands werden abgelehnt",
        "`headersHelper` são rejeitados",
        "`headersHelper` commands は、別途 trusted-execution design が必要なため拒否されます",
        "`headersHelper` commands 会被拒绝",
        "| Dynamic `headersHelper` commands | Not supported.",
        "| Comandos dinámicos `headersHelper` | No compatibles.",
        "| Commandes dynamiques `headersHelper` | Non prises en charge.",
        "| Dynamische `headersHelper`-Commands | Nicht unterstützt.",
        "| Comandos dinâmicos `headersHelper` | Não compatíveis.",
        "| 動的 `headersHelper` commands | 未対応。",
        "| 动态 `headersHelper` commands | 不支持。",
    ]
    failures: list[str] = []

    for root in LOCALE_DOC_ROOTS:
        for relative_path, needles in required_by_page.items():
            path = root / relative_path
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle not in text:
                    failures.append(f"{path.relative_to(PROJECT_ROOT)}: missing {needle!r}")
            for phrase in forbidden:
                if phrase in text:
                    failures.append(f"{path.relative_to(PROJECT_ROOT)}: forbidden {phrase!r}")

    assert not failures, "\n".join(failures)


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
