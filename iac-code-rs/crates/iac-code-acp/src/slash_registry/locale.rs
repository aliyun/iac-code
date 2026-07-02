use std::collections::BTreeMap;
use std::sync::OnceLock;

use iac_code_config::i18n::detect_language;

const ZH_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/zh/LC_MESSAGES/messages.po");
const ES_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/es/LC_MESSAGES/messages.po");
const FR_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/fr/LC_MESSAGES/messages.po");
const DE_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/de/LC_MESSAGES/messages.po");
const JA_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/ja/LC_MESSAGES/messages.po");
const PT_MESSAGES_PO: &str =
    include_str!("../../../../resources/i18n/locales/pt/LC_MESSAGES/messages.po");

pub(super) fn tr_acp_unsupported_command(command: &str, supported: &str) -> String {
    tr("Command '/{cmd_name}' is not supported over ACP. Supported commands: {supported}")
        .replace("{cmd_name}", command)
        .replace("{supported}", supported)
}

pub(super) fn tr_acp_compaction_result(
    original_tokens: u64,
    compacted_tokens: u64,
    usage: &str,
) -> String {
    match compacted_tokens.cmp(&original_tokens) {
        std::cmp::Ordering::Less => tr_acp_compaction_success_reduction(
            original_tokens,
            compacted_tokens,
            &format!(
                "{}%",
                compaction_change_percent(original_tokens, compacted_tokens)
            ),
            usage,
        ),
        std::cmp::Ordering::Greater => tr_acp_compaction_success_increase(
            original_tokens,
            compacted_tokens,
            &format!(
                "{}%",
                compaction_change_percent(original_tokens, compacted_tokens)
            ),
            usage,
        ),
        std::cmp::Ordering::Equal => {
            tr_acp_compaction_success_unchanged(original_tokens, compacted_tokens, usage)
        }
    }
}

fn compaction_change_percent(original_tokens: u64, compacted_tokens: u64) -> u64 {
    if original_tokens == 0 {
        return 0;
    }
    let delta = original_tokens.abs_diff(compacted_tokens) as f64 / original_tokens as f64 * 100.0;
    delta.round() as u64
}

fn tr_acp_compaction_success_reduction(
    original_tokens: u64,
    compacted_tokens: u64,
    percent: &str,
    usage: &str,
) -> String {
    tr(
        "Context compacted: {original} \u{2192} {compacted} tokens ({percent} reduction). Context usage: {usage}",
    )
    .replace("{original}", &original_tokens.to_string())
    .replace("{compacted}", &compacted_tokens.to_string())
    .replace("{percent}", percent)
    .replace("{usage}", usage)
}

fn tr_acp_compaction_success_increase(
    original_tokens: u64,
    compacted_tokens: u64,
    percent: &str,
    usage: &str,
) -> String {
    tr(
        "Context compacted: {original} \u{2192} {compacted} tokens ({percent} increase). Context usage: {usage}",
    )
    .replace("{original}", &original_tokens.to_string())
    .replace("{compacted}", &compacted_tokens.to_string())
    .replace("{percent}", percent)
    .replace("{usage}", usage)
}

fn tr_acp_compaction_success_unchanged(
    original_tokens: u64,
    compacted_tokens: u64,
    usage: &str,
) -> String {
    tr(
        "Context compacted: {original} \u{2192} {compacted} tokens (no token change). Context usage: {usage}",
    )
    .replace("{original}", &original_tokens.to_string())
    .replace("{compacted}", &compacted_tokens.to_string())
    .replace("{usage}", usage)
}

pub(super) fn tr_value(message: &'static str, key: &str, value: &str) -> String {
    tr(message).replace(&format!("{{{key}}}"), value)
}

pub(super) fn tr_name(message: &'static str, name: &str) -> String {
    tr(message).replace("{name}", name)
}

pub(super) fn tr_known_agent_error(error: &str) -> String {
    match error {
        "Memory manager is unavailable." => tr("Memory manager is unavailable."),
        "Rename is only available after a session is created." => {
            tr("Rename is only available after a session is created.")
        }
        _ => error.to_owned(),
    }
}

pub(super) fn tr_turns(message: &'static str, turns: impl std::fmt::Display) -> String {
    tr(message).replace("{turns}", &turns.to_string())
}

pub(super) fn tr(message: &'static str) -> String {
    let language = detect_language();
    if language == "en" {
        return message.to_owned();
    }
    translation_catalogs()
        .get(language.as_str())
        .and_then(|catalog| catalog.get(message))
        .filter(|translated| !translated.is_empty())
        .cloned()
        .unwrap_or_else(|| message.to_owned())
}

fn translation_catalogs() -> &'static BTreeMap<&'static str, BTreeMap<String, String>> {
    static CATALOGS: OnceLock<BTreeMap<&'static str, BTreeMap<String, String>>> = OnceLock::new();
    CATALOGS.get_or_init(|| {
        let mut catalogs = BTreeMap::new();
        catalogs.insert("zh", parse_po_catalog(ZH_MESSAGES_PO));
        catalogs.insert("es", parse_po_catalog(ES_MESSAGES_PO));
        catalogs.insert("fr", parse_po_catalog(FR_MESSAGES_PO));
        catalogs.insert("de", parse_po_catalog(DE_MESSAGES_PO));
        catalogs.insert("ja", parse_po_catalog(JA_MESSAGES_PO));
        catalogs.insert("pt", parse_po_catalog(PT_MESSAGES_PO));
        catalogs
    })
}

#[derive(Copy, Clone)]
enum PoField {
    None,
    MsgId,
    MsgStr,
    Ignore,
}

fn parse_po_catalog(content: &str) -> BTreeMap<String, String> {
    let mut catalog = BTreeMap::new();
    let mut msgid: Option<String> = None;
    let mut msgstr: Option<String> = None;
    let mut field = PoField::None;

    for line in content.lines() {
        let line = line.trim_start();
        if line.is_empty() {
            flush_po_entry(&mut catalog, &mut msgid, &mut msgstr);
            field = PoField::None;
            continue;
        }
        if line.starts_with('#') {
            continue;
        }
        if let Some(raw) = line.strip_prefix("msgid ") {
            flush_po_entry(&mut catalog, &mut msgid, &mut msgstr);
            msgid = parse_po_quoted(raw);
            msgstr = None;
            field = PoField::MsgId;
            continue;
        }
        if line.starts_with("msgid_plural ") {
            field = PoField::Ignore;
            continue;
        }
        if let Some(raw) = line.strip_prefix("msgstr ") {
            msgstr = parse_po_quoted(raw);
            field = PoField::MsgStr;
            continue;
        }
        if line.starts_with("msgstr[") {
            field = PoField::Ignore;
            continue;
        }
        if line.starts_with('"') {
            if let Some(fragment) = parse_po_quoted(line) {
                match field {
                    PoField::MsgId => {
                        if let Some(value) = &mut msgid {
                            value.push_str(&fragment);
                        }
                    }
                    PoField::MsgStr => {
                        if let Some(value) = &mut msgstr {
                            value.push_str(&fragment);
                        }
                    }
                    PoField::None | PoField::Ignore => {}
                }
            }
        }
    }
    flush_po_entry(&mut catalog, &mut msgid, &mut msgstr);
    catalog
}

fn flush_po_entry(
    catalog: &mut BTreeMap<String, String>,
    msgid: &mut Option<String>,
    msgstr: &mut Option<String>,
) {
    if let (Some(id), Some(translated)) = (msgid.take(), msgstr.take()) {
        if !id.is_empty() && !translated.is_empty() {
            catalog.insert(id, translated);
        }
    }
}

fn parse_po_quoted(raw: &str) -> Option<String> {
    let raw = raw.trim();
    if !raw.starts_with('"') {
        return None;
    }
    let mut output = String::new();
    let mut escaped = false;
    for ch in raw[1..].chars() {
        if escaped {
            match ch {
                'n' => output.push('\n'),
                'r' => output.push('\r'),
                't' => output.push('\t'),
                '"' => output.push('"'),
                '\\' => output.push('\\'),
                other => output.push(other),
            }
            escaped = false;
            continue;
        }
        match ch {
            '\\' => escaped = true,
            '"' => return Some(output),
            other => output.push(other),
        }
    }
    Some(output)
}
