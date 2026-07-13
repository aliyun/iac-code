"""Tests for internationalization (i18n) translation completeness.

This module ensures all language translations are complete and cover
all msgid entries from the .pot template file.
"""

import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from string import Formatter

import pytest
from babel.messages.pofile import read_po

from iac_code.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent
I18N_DIR = PROJECT_ROOT / "src" / "iac_code" / "i18n"
POT_FILE = I18N_DIR / "messages.pot"
LOCALES_DIR = I18N_DIR / "locales"

MEMORY_COMMAND_MSGIDS = {
    "Usage: /memory-folder [<name>|search <query>|delete <name>|help]",
    "Saved memories:",
    "No memories saved yet.",
    "Matching memories:",
    "No matching memories.",
    "Memory '{name}' not found.",
    "Memory '{name}' deleted.",
    "Memory manager is unavailable.",
    "Edit memory files",
    "View and manage persistent memories",
    "[<name>|search <query>|delete <name>|help]",
    "Search saved memories",
    "Delete a saved memory",
    "Show memory command help",
    "Saved memory",
}

PIPELINE_USER_VISIBLE_MSGIDS = {
    "Waiting for candidate data...",
    "Cost details",
    "Pipeline completed. Normal chat is now active.",
    "Judging your input...",
    "Interrupt handling",
    (
        "Complete the current step by calling this tool to submit the conclusion. "
        "If you need to roll back to an earlier step, set rollback_request."
    ),
    (
        "Optional iac-code pipeline event extension for state snapshots, replay, interrupts, "
        "and parallel candidate streams."
    ),
    "Selling",
    "Intent parsing",
    "Architecture planning",
    "Evaluate candidates",
    "Confirm and select",
    "Deploying",
    "Evaluate candidate",
    "Template generation",
    "Review",
    "Cost estimation",
    "Current step",
    "Complete step",
    "backup unavailable",
    "Pipeline backup is blocked; the pipeline is paused and recoverable: {error}",
    "Ask user question",
    "Show architecture diagram",
    "Show candidate details",
    "InfraGuard scan",
    "Run InfraGuard static scan and return structured JSON results.",
    "review step",
    "configured feature",
    "Direct binary download",
    "Go install",
    "Homebrew",
    "Installing prerequisites...",
    "Downloading prerequisites...",
    "Initializing prerequisites...",
    "Installing prerequisites with {installer}...",
    (
        "Pipeline prerequisite {name} is missing and no configured installer is usable; "
        "{feature} will be skipped for this run."
    ),
    (
        "Pipeline prerequisite {name} is missing; it is required for {feature}. "
        "Choose an installer or skip this feature for this run."
    ),
    "Skip {feature} for this run",
    "command timed out after {seconds} seconds",
    "Version check failed for {name}: {reason}",
    "Could not determine {name} version from output.",
    "{name} version {version} is lower than required {minimum}.",
    "error",
    "passed",
    "failed",
    "completed",
    "1 finding",
    "{count} findings",
    "blocking {count}",
    "line {line}",
    "Reason: {reason}",
    "Recommendation: {recommendation}",
    "Snippet: {snippet}",
    "Command: {command}",
    "Status: {status}",
    "Error: {error}",
    "File: {file_path}",
    "Mode: {mode}",
    "Exit code: {exit_code}",
    "Ignore waivers: {value}",
    "Blocking severities: {severities}",
    "Blocking findings: {count}",
    "Aspects: {aspects}",
    "Policies:",
    "Summary:",
    "Severity counts: {counts}",
    "Stderr: {stderr}",
    "Findings:",
    "No findings.",
    (
        "reviewing ran write_file/edit_file after ros_validate_template; "
        "rerun ros_validate_template and infraguard_scan for the same file_path."
    ),
    "reviewing must validate the repaired template with ros_validate_template for the same file_path.",
    (
        "reviewing ran write_file/edit_file after the final InfraGuard scan; "
        "rerun ros_validate_template and infraguard_scan for the same file_path."
    ),
    "reviewing must finish with a passing InfraGuard scan for the same file_path.",
    (
        "This input still lacks a clear cloud resource, deployment target, or operations constraint; "
        "clarify with the user first."
    ),
    (
        "The current flow only supports Alibaba Cloud deployment requests; ask the user to change the target "
        "to Alibaba Cloud or confirm that it should not be handled for now."
    ),
    "Low-confidence intent cannot be completed directly; clarify with the user first.",
    (
        "This input is not a deployment or cloud resource request; ask the user to provide a deployment target "
        "or confirm that it should not be handled for now."
    ),
    "A successful deployment must wait until ros_deploy returns CREATE_COMPLETE.",
    ("{feature} skipped\nPrerequisite {name} failed, so {feature} is disabled for this run.\nReason: {reason}"),
    'Generated the architecture diagram for "{candidate_name}".',
    'Displayed details for "{candidate_name}".',
}

MCP_HOST_ENHANCE_USER_VISIBLE_MSGIDS = {
    "Manage MCP servers",
    "[enable|disable|reconnect [server-name] [--scope scope] [--source-path path]]",
    "MCP command requires a context.",
    'MCP server "{name}" not found',
    "enabled",
    "disabled",
    "All MCP servers are already {state}",
    "Enabled",
    "Disabled",
    "{verb} {count} MCP server(s)",
    'MCP server "{name}" {state}',
    "Error: {error}",
    "Successfully reconnected to {name}",
    "{name} requires authentication. Use /mcp to authenticate.",
    "Failed to reconnect to {name}",
    "--scope or --source-path cannot be used with /mcp {command} all.",
    "--scope requires a value.",
    "--source-path requires a value.",
    "Persisted MCP config file path.",
    "Interactive MCP manager requires a TTY. Use `iac-code mcp list` or MCP quick commands.",
    "Command or remote URL.",
    "Transport type: stdio, http, sse, ws.",
    "Remote MCP URL for http/sse/ws.",
    "HTTP header KEY=VALUE or Name: Value. Can be repeated.",
    (
        "Warning: {operand!r} looks like a URL. Use --transport http, --transport sse, "
        "or --transport ws for remote MCP servers."
    ),
    "Use either --url or a positional URL for remote MCP servers, not both.",
    "Remote MCP servers accept one positional URL, not command arguments.",
    "Connect briefly and show MCP health.",
    "Show configured MCP servers without health checks.",
    "Use either --check or --config-only, not both.",
    "Show configured MCP server JSON only.",
    "MCP health check failed: {error}",
    "No diagnostic was returned.",
    "MCP server name.",
    "Reconnect all persisted MCP servers.",
    "Use either a server name or --all, not both.",
    "--scope cannot be used with mcp reconnect --all.",
    "MCP server name is required unless --all is used.",
    "Disabled MCP server {name!r}.",
    "Enabled MCP server {name!r}.",
    "OAuth authorization was cancelled.",
    "yes",
    "no",
    "Browser opened: {status}",
    "Authorization URL: {url}",
    (
        "Paste the callback URL or authorization code for MCP server {name!r}, "
        "or press Enter to wait for the loopback callback:"
    ),
    "MCP reconnect failed: {error}",
    "MCP server {name!r} not found in persisted MCP config.",
    "MCP server {name!r} exists in multiple persisted scopes. Re-run with one of:",
    (
        "Unknown MCP option {option!r}. Put subprocess flags after a command, for example: "
        "iac-code mcp add NAME -- npx --yes mcp-server."
    ),
    "Next: run `{command}` to check MCP server health.",
    "Next: run `{command}` to authenticate this MCP server.",
    "{option} expects {expected}, got {value!r}.",
    "MCP server {server!r} is closing.",
    "MCP headers helper for server {server!r} could not be parsed: {error}",
    "MCP headers helper for server {server!r} is empty.",
    "MCP headers helper for server {server!r} failed to start: {error}",
    "timed out after {seconds:g} seconds",
    "{stream} output too large",
    "exited with status {status}",
    "returned invalid JSON",
    "must return a JSON object",
    "must return string header names and values",
    "MCP headers helper for server {server!r} {reason}.",
    "{message}\nMCP headers helper stderr:\n{stderr}",
    "MCP HTTP session expired for server {server!r}; reconnect required.",
    "authentication required",
    "{} Required scopes: {}",
    "MCP HTTP session expired; reconnect required.",
    "MCP server disabled.",
    "Project MCP server pending approval.",
    "{} call failed: {}",
    "MCP server {server!r} oauth.clientMetadataUrl must be an HTTPS URL with a non-root pathname.",
    "MCP server {server!r} oauth.callbackPort must be between 0 and 65535.",
    "The installed MCP SDK does not support OAuth client metadata URLs.",
    "required scopes: {scopes}",
    "OAuth dynamic client registration requires a remote MCP server URL.",
    "Timed out waiting for MCP OAuth authorization URL.",
    "OAuth provider did not produce an authorization URL.",
    "OAuth manual callback input was empty.",
    "Saved large MCP text output as {artifact_id} ({chars} chars, {bytes} bytes).",
    "Read the full output from {path}.",
    "MCP skill alias {alias!r} conflicts with an existing command.",
    "MCP server {server!r} field headersHelper is only supported for http and sse transports.",
    ("MCP server {server!r} WebSocket transport url must be a ws:// or wss:// URL with a host."),
    (
        "MCP server {server!r} WebSocket transport field {field} is not supported because "
        "the installed MCP SDK websocket_client accepts only a URL."
    ),
    "{label}{suffix} ({choices}): ",
    "{label}{suffix} (yes/no): ",
    "{label}{suffix}: ",
    "MCP server {server!r} requested user action.",
    "Press Enter when complete, type 'decline' to decline, or 'cancel' to cancel: ",
    "Type 'accept' to accept, 'decline' to decline, or 'cancel' to cancel: ",
    "No MCP servers configured. Run `iac-code mcp --help` to learn more.",
    "No MCP servers configured.",
    "Run `iac-code mcp --help` to add or inspect MCP servers.",
    "Enter select",
    "Esc close",
    "Up/Down navigate",
    "Actions",
    "Authenticating {name}",
    "Copied!",
    "press c to copy",
    "c copy URL",
    "If your browser does not open automatically, copy this URL manually ({hint}).",
    "Waiting for OAuth callback.",
    "If the redirect page shows a connection error, paste the URL from your browser address bar.",
    "Enter submit",
    "Esc cancel",
    "This may take a few moments.",
    "Failed to {action} MCP server {name!r}: {error}",
    "Enable",
    "Disable",
    "Authenticate",
    "Re-authenticate",
    "Clear authentication",
    "Reconnect",
    "View tools",
    "View resources",
    "View prompts",
    "Resources for {name}",
    "1 resource",
    "{count} resources",
    "No resources available",
    "Name: {name}",
    "MIME type: {mime_type}",
    "Prompts for {name}",
    "1 prompt",
    "{count} prompts",
    "No prompts available",
    "Prompt name: {name}",
    "Arguments:",
    "Refreshed MCP status.",
    "Authentication successful. Enable {name} to connect.",
    "Restarting MCP server process",
    "Establishing connection to MCP server",
    "Error reconnecting to {name}: {error}",
    "{name} requires authentication. Use the 'Authenticate' option.",
    "Authentication successful. Connected to {name}.",
    "Authentication successful. Reconnected to {name}.",
    "Authentication cleared for {name}.",
    "MCP Config Diagnostics",
    "For help configuring MCP servers, run `iac-code mcp --help`.",
    "Failed to parse",
    "Contains warnings",
    "Location: {path}",
    "{scope} MCPs",
    "Auth",
    "URL",
    "Capabilities",
    "Failure",
    "Latest failure",
    "Latest refresh failure",
    "Tools refresh",
    "Resources refresh",
    "Prompts refresh",
    "{capability} refresh",
    "prompts",
}

SESSION_BACKUP_USER_VISIBLE_MSGIDS = {
    "A2A pipeline sidecar owner is unavailable",
    "A2A pipeline sidecar restore failed: status={status}, reason={reason}",
    "Failed to persist A2A pipeline snapshot",
    "Session backup requires a supported session layout.",
    "Unrepairable A2A pipeline journal tail",
    "backup root",
    "invalid session layout version",
    "invalid session metadata",
    "session source",
    "unknown",
}

ROS_DEPLOYMENT_REJECTION_MSGID = (
    "ROS pipeline calls for {action} must use the dedicated ros_deploy tool instead of aliyun_api. "
    "Do not call the raw ROS deployment API directly."
)


def _get_all_msgids_from_pot(pot_file: Path) -> set[str]:
    """Extract all msgids from a .pot template file.

    Skips plural forms (message.id as tuple) and empty msgids.

    Args:
        pot_file: Path to the .pot file.

    Returns:
        A set of all msgid strings that need translation.
    """
    with open(pot_file, "r", encoding="utf-8") as f:
        catalog = read_po(f)

    return {message.id for message in catalog if message.id and isinstance(message.id, str)}


def _get_all_translations_from_po(po_file: Path) -> dict[str, str]:
    """Extract msgid->msgstr mappings from a .po file.

    Fuzzy entries are treated as untranslated (empty msgstr).
    Plural forms are skipped.

    Args:
        po_file: Path to the .po file.

    Returns:
        A dictionary mapping msgid to msgstr.
    """
    with open(po_file, "r", encoding="utf-8") as f:
        catalog = read_po(f)

    result = {}
    for message in catalog:
        if not message.id or not isinstance(message.id, str):
            continue
        if "fuzzy" in message.flags:
            result[message.id] = ""  # Treat fuzzy as untranslated
        else:
            result[message.id] = message.string
    return result


def _format_fields(value: str) -> Counter[str]:
    fields: Counter[str] = Counter()
    for literal_text, field_name, format_spec, conversion in Formatter().parse(value):
        _ = literal_text
        if field_name is None:
            continue
        token = "{"
        token += field_name
        if conversion:
            token += "!{}".format(conversion)
        if format_spec:
            token += ":{}".format(format_spec)
        token += "}"
        fields[token] += 1
    return fields


def test_format_fields_tracks_empty_positional_placeholders() -> None:
    assert _format_fields("{} Required scopes: {}") == Counter({"{}": 2})


def _discover_language_dirs() -> list[Path]:
    """Discover all language directories in the locales folder.

    Returns:
        A list of paths to language directories (e.g., zh, en).
    """
    if not LOCALES_DIR.exists():
        return []

    return [
        d for d in LOCALES_DIR.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != DEFAULT_LANGUAGE
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_pot_file_exists():
    """Verify that the .pot template file exists."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    assert POT_FILE.is_file(), f"POT path exists but is not a file: {POT_FILE}"


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_translation_source_references_do_not_include_line_numbers():
    """Avoid noisy PO churn from line-number-only source changes."""
    catalog_files = [POT_FILE, *LOCALES_DIR.glob("*/LC_MESSAGES/messages.po")]
    references_with_line_numbers = []

    for catalog_file in catalog_files:
        for line_number, line in enumerate(catalog_file.read_text(encoding="utf-8").splitlines(), start=1):
            has_line_number = any(reference.rsplit(":", 1)[-1].isdigit() for reference in line.split()[1:])
            if line.startswith("#:") and has_line_number:
                references_with_line_numbers.append(f"{catalog_file.relative_to(PROJECT_ROOT)}:{line_number}: {line}")

    displayed_references = references_with_line_numbers[:20]
    if len(references_with_line_numbers) > len(displayed_references):
        displayed_references.append(f"... and {len(references_with_line_numbers) - len(displayed_references)} more")

    assert not references_with_line_numbers, (
        "Translation catalogs should use file-only source references. "
        "Run: uv run pybabel extract -F babel.cfg --add-location=file -o src/iac_code/i18n/messages.pot .\n"
        + "\n".join(displayed_references)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_pot_is_up_to_date():
    """Verify .pot file is in sync with source code _() calls."""
    with tempfile.NamedTemporaryFile(suffix=".pot", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["uv", "run", "pybabel", "extract", "-F", "babel.cfg", "--add-location=file", "-o", tmp_path, "."],
            cwd=str(PROJECT_ROOT),
            check=True,
            capture_output=True,
            timeout=60,
        )

        # Parse both .pot files
        current_msgids = _get_all_msgids_from_pot(POT_FILE)
        fresh_msgids = _get_all_msgids_from_pot(Path(tmp_path))

        missing_in_pot = fresh_msgids - current_msgids
        extra_in_pot = current_msgids - fresh_msgids

        errors = []
        if missing_in_pot:
            errors.append(f"msgids in source but missing from .pot ({len(missing_in_pot)}):")
            for mid in sorted(missing_in_pot):
                errors.append(f"  - {mid!r}")
        if extra_in_pot:
            errors.append(f"msgids in .pot but not found in source ({len(extra_in_pot)}):")
            for mid in sorted(extra_in_pot):
                errors.append(f"  - {mid!r}")

        if errors:
            pytest.fail(
                "messages.pot is out of date. Run: "
                "uv run pybabel extract -F babel.cfg --add-location=file "
                "-o src/iac_code/i18n/messages.pot .\n" + "\n".join(errors)
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_all_languages_have_po_files():
    """Verify that each language directory has a valid messages.po file."""
    language_dirs = _discover_language_dirs()

    if not language_dirs:
        pytest.skip("No language directories found")

    missing_po_files = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        if not po_file.exists():
            missing_po_files.append(f"{lang_dir.name}/LC_MESSAGES/messages.po")

    assert not missing_po_files, f"Missing .po files for languages: {missing_po_files}"


def test_supported_languages_match_locale_dirs():
    """Verify supported languages are the default language plus locale directories."""
    language_dirs = _discover_language_dirs()
    locale_codes = {lang_dir.name for lang_dir in language_dirs}

    assert len(SUPPORTED_LANGUAGES) == 7
    assert set(SUPPORTED_LANGUAGES) == {DEFAULT_LANGUAGE, *locale_codes}


def test_mo_compilation_valid():
    """Verify that .po files can be compiled to .mo without errors.

    Catches issues like incompatible placeholder flags that prevent
    compilation and leave the .mo stale.
    """
    import io

    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    language_dirs = _discover_language_dirs()
    if not language_dirs:
        pytest.skip("No language directories found")

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        if not po_file.exists():
            continue

        try:
            with open(po_file, "rb") as f:
                catalog = read_po(f)
            buf = io.BytesIO()
            write_mo(buf, catalog)
        except Exception as e:
            errors.append(f"{lang_dir.name}: compilation failed — {e}")

    if errors:
        pytest.fail(".po files have compilation errors:\n" + "\n".join(errors))


def test_translation_completeness():
    """Verify all translations are complete for all languages.

    This test checks that:
    1. All msgid entries from .pot have corresponding entries in each .po file
    2. All msgstr entries are non-empty (actually translated)
    3. Fuzzy entries are treated as untranslated
    """
    # First, ensure pot file exists
    if not POT_FILE.exists():
        pytest.skip("POT file does not exist")

    # Get all msgids from template
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)

    if not pot_msgids:
        pytest.skip("No msgid entries found in POT file")

    # Discover all language directories
    language_dirs = _discover_language_dirs()

    if not language_dirs:
        pytest.skip("No language directories found")

    # Track incomplete translations per language
    all_errors: dict[str, list[str]] = {}

    for lang_dir in language_dirs:
        lang_code = lang_dir.name
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"

        # Skip if .po file doesn't exist (other test covers this)
        if not po_file.exists():
            continue

        # Get all translations for this language
        translations = _get_all_translations_from_po(po_file)

        missing_entries = []  # msgid not in .po at all
        empty_translations = []  # msgid in .po but msgstr is empty

        for msgid in sorted(pot_msgids):
            msgstr = translations.get(msgid)
            if msgstr is None:
                missing_entries.append(msgid)
            elif not msgstr.strip():
                empty_translations.append(msgid)

        errors = []
        if missing_entries:
            errors.append(f"  Missing entries ({len(missing_entries)}):")
            for mid in missing_entries:
                errors.append(f"    - {mid!r}")
        if empty_translations:
            errors.append(f"  Empty translations ({len(empty_translations)}):")
            for mid in empty_translations:
                errors.append(f"    - {mid!r}")

        if errors:
            all_errors[lang_code] = errors

    # Assert no incomplete translations
    if all_errors:
        error_messages = []
        for lang, errors in all_errors.items():
            error_messages.append(f"Language '{lang}' has incomplete translations:")
            error_messages.extend(errors)
        pytest.fail("\n".join(error_messages))


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_memory_command_translations_are_complete():
    """Verify /memory-specific strings are translated, not copied as placeholders."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = MEMORY_COMMAND_MSGIDS - pot_msgids
    assert not missing_from_pot, f"/memory msgids missing from messages.pot: {sorted(missing_from_pot)}"

    language_dirs = _discover_language_dirs()
    assert language_dirs, "No language directories found"

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        translations = _get_all_translations_from_po(po_file)
        for msgid in sorted(MEMORY_COMMAND_MSGIDS):
            msgstr = translations.get(msgid, "").strip()
            if not msgstr:
                errors.append(f"{lang_dir.name}: missing translation for {msgid!r}")
            elif msgstr == msgid:
                errors.append(f"{lang_dir.name}: untranslated placeholder for {msgid!r}")
            elif _format_fields(msgstr) != _format_fields(msgid):
                errors.append(
                    f"{lang_dir.name}: placeholder mismatch for {msgid!r}: "
                    f"expected {sorted(_format_fields(msgid))}, got {sorted(_format_fields(msgstr))}"
                )

    assert not errors, "\n".join(errors)


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_pipeline_user_visible_translations_are_complete():
    """Pipeline UI/tool strings are visible in the terminal and must be localized."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = PIPELINE_USER_VISIBLE_MSGIDS - pot_msgids
    assert not missing_from_pot, f"Pipeline msgids missing from messages.pot: {sorted(missing_from_pot)}"

    language_dirs = _discover_language_dirs()
    assert language_dirs, "No language directories found"

    errors = []
    for lang_dir in language_dirs:
        po_file = lang_dir / "LC_MESSAGES" / "messages.po"
        translations = _get_all_translations_from_po(po_file)
        for msgid in sorted(PIPELINE_USER_VISIBLE_MSGIDS):
            msgstr = translations.get(msgid, "").strip()
            if not msgstr:
                errors.append(f"{lang_dir.name}: missing translation for {msgid!r}")
            elif msgstr == msgid:
                errors.append(f"{lang_dir.name}: untranslated placeholder for {msgid!r}")
            elif _format_fields(msgstr) != _format_fields(msgid):
                errors.append(
                    f"{lang_dir.name}: placeholder mismatch for {msgid!r}: "
                    f"expected {sorted(_format_fields(msgid))}, got {sorted(_format_fields(msgstr))}"
                )

    assert not errors, "\n".join(errors)


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_mcp_host_enhance_user_visible_translations_are_complete():
    """MCP host command, OAuth, diagnostics, and /mcp UI strings must be localized."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = MCP_HOST_ENHANCE_USER_VISIBLE_MSGIDS - pot_msgids
    assert not missing_from_pot, f"MCP host msgids missing from messages.pot: {sorted(missing_from_pot)}"

    errors = []
    for lang_dir in _discover_language_dirs():
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        for msgid in sorted(MCP_HOST_ENHANCE_USER_VISIBLE_MSGIDS):
            msgstr = translations.get(msgid, "").strip()
            if not msgstr:
                errors.append(f"{lang_dir.name}: missing translation for {msgid!r}")
            elif msgstr == msgid:
                errors.append(f"{lang_dir.name}: untranslated placeholder for {msgid!r}")
            elif _format_fields(msgstr) != _format_fields(msgid):
                errors.append(
                    f"{lang_dir.name}: placeholder mismatch for {msgid!r}: "
                    f"expected {sorted(_format_fields(msgid))}, got {sorted(_format_fields(msgstr))}"
                )

    assert not errors, "\n".join(errors)


def test_mcp_manager_authenticate_option_reference_uses_localized_label():
    msgid = "{name} requires authentication. Use the 'Authenticate' option."
    for lang_dir in _discover_language_dirs():
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        auth_label = translations.get("Authenticate", "").strip()
        message = translations.get(msgid, "").strip()
        assert auth_label, f"{lang_dir.name}: missing translation for 'Authenticate'"
        assert message, f"{lang_dir.name}: missing translation for {msgid!r}"
        assert auth_label in message, f"{lang_dir.name}: auth prompt does not reference localized label {auth_label!r}"


def test_ros_deployment_rejection_translations_preserve_format_fields():
    """The pipeline safety error must not fail while formatting localized text."""
    language_dirs = _discover_language_dirs()
    assert language_dirs, "No language directories found"

    errors = []
    for lang_dir in language_dirs:
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        msgstr = translations.get(ROS_DEPLOYMENT_REJECTION_MSGID, "")
        if not msgstr:
            errors.append(f"{lang_dir.name}: missing translation")
            continue
        if "{tool_name}" in msgstr:
            errors.append(f"{lang_dir.name}: stale {{tool_name}} placeholder")
            continue
        try:
            formatted = msgstr.format(action="CreateStack")
        except (IndexError, KeyError, ValueError) as exc:
            errors.append(f"{lang_dir.name}: format failed: {exc}")
            continue
        if "CreateStack" not in formatted:
            errors.append(f"{lang_dir.name}: formatted action missing")

    assert not errors, "\n".join(errors)


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_session_backup_labels_are_translatable():
    """Session backup validation errors interpolate labels, so guard against raw English labels."""
    assert POT_FILE.exists(), f"POT file not found at {POT_FILE}"
    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = SESSION_BACKUP_USER_VISIBLE_MSGIDS - pot_msgids
    assert not missing_from_pot, "Session backup msgids missing from messages.pot: {}".format(sorted(missing_from_pot))

    errors = []
    for lang_dir in _discover_language_dirs():
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        for msgid in sorted(SESSION_BACKUP_USER_VISIBLE_MSGIDS):
            msgstr = translations.get(msgid, "").strip()
            if not msgstr:
                errors.append(f"{lang_dir.name}: missing translation for {msgid!r}")
            elif msgstr == msgid:
                errors.append(f"{lang_dir.name}: untranslated placeholder for {msgid!r}")

    assert not errors, "\n".join(errors)


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_aliyun_credential_labels_are_translatable():
    """Aliyun auth menu labels come from data tables, so guard against dynamic gettext misses."""
    from iac_code.services.providers.aliyun import MODE_DISPLAY_NAMES, MODE_FIELDS

    required_msgids = set(MODE_DISPLAY_NAMES.values())
    for mode_fields in MODE_FIELDS.values():
        required_msgids.update(label for _field_name, label, _sensitive in mode_fields)

    pot_msgids = _get_all_msgids_from_pot(POT_FILE)
    missing_from_pot = sorted(required_msgids - pot_msgids)
    assert not missing_from_pot, "Aliyun credential labels missing from messages.pot: {}".format(missing_from_pot)

    missing_or_empty_by_language: dict[str, list[str]] = {}
    for lang_dir in _discover_language_dirs():
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        missing_or_empty = sorted(msgid for msgid in required_msgids if not translations.get(msgid))
        if missing_or_empty:
            missing_or_empty_by_language[lang_dir.name] = missing_or_empty

    assert not missing_or_empty_by_language, "Aliyun credential labels missing translations: {}".format(
        missing_or_empty_by_language
    )


@pytest.mark.skipif(sys.platform == "win32", reason="messages.pot not generated on Windows")
def test_session_name_error_messages_are_translated():
    """Session rename validation errors are user-facing and must not stay English-only."""
    required_msgids = {
        "Session name must match {pattern}",
        "Session name already exists in this project: {name}",
    }
    language_dirs = _discover_language_dirs()
    if not language_dirs:
        pytest.skip("No language directories found")

    untranslated: list[str] = []
    for lang_dir in language_dirs:
        translations = _get_all_translations_from_po(lang_dir / "LC_MESSAGES" / "messages.po")
        for msgid in sorted(required_msgids):
            msgstr = translations.get(msgid, "")
            if not msgstr.strip() or msgstr == msgid:
                untranslated.append(f"{lang_dir.name}: {msgid!r}")

    assert not untranslated


class TestDetectWindowsUILanguage:
    """_detect_windows_ui_language wraps GetUserDefaultLocaleName via ctypes."""

    def test_returns_two_letter_code_for_zh_cn(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        def fake_get_user_default_locale_name(buf, size):
            for i, ch in enumerate("zh-CN"):
                buf[i] = ch
            return len("zh-CN") + 1

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = fake_get_user_default_locale_name
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() == "zh"

    def test_returns_none_when_api_fails(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        def fake_get_user_default_locale_name(buf, size):
            return 0

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = fake_get_user_default_locale_name
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() is None

    def test_returns_none_on_oserror(self, monkeypatch):
        import ctypes
        import types
        from unittest.mock import MagicMock

        from iac_code.i18n import _detect_windows_ui_language

        mock_kernel32 = MagicMock()
        mock_kernel32.GetUserDefaultLocaleName = MagicMock(side_effect=OSError("boom"))
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=mock_kernel32), raising=False)

        assert _detect_windows_ui_language() is None


class TestDetectLanguage:
    """_detect_language env vars + Windows fallback chain."""

    def test_env_var_zh(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert _detect_language() == "zh"

    def test_env_var_unsupported_falls_through(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("LANG", "ko_KR.UTF-8")
        monkeypatch.setattr("iac_code.i18n.sys.platform", "linux")
        assert _detect_language() == "en"

    def test_windows_path_uses_kernel32(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "win32")
        monkeypatch.setattr(
            "iac_code.i18n._detect_windows_ui_language",
            lambda: "zh",
        )
        assert _detect_language() == "zh"

    def test_windows_kernel32_returns_unsupported(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "win32")
        monkeypatch.setattr(
            "iac_code.i18n._detect_windows_ui_language",
            lambda: "ko",
        )
        assert _detect_language() == "en"

    def test_all_empty_returns_default(self, monkeypatch):
        from iac_code.i18n import _detect_language

        for v in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr("iac_code.i18n.sys.platform", "linux")
        assert _detect_language() == "en"
