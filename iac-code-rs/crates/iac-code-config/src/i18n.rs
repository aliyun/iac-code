use std::env;
#[cfg(target_os = "macos")]
use std::sync::OnceLock;

pub const DEFAULT_LANGUAGE: &str = "en";
pub const SUPPORTED_LANGUAGES: &[&str] = &["en", "zh", "es", "fr", "de", "ja", "pt"];

const LANGUAGE_ENV_VARS: &[&str] = &["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"];

pub fn detect_language() -> String {
    let env_values = LANGUAGE_ENV_VARS
        .iter()
        .map(|name| (*name, env::var(name).ok()))
        .collect::<Vec<_>>();
    let system_locales = system_locale_candidates();

    detect_language_from_sources(
        env_values
            .iter()
            .map(|(name, value)| (*name, value.as_deref())),
        system_locales.iter().map(String::as_str),
    )
}

pub fn detect_language_from_sources<'a>(
    env_values: impl IntoIterator<Item = (&'a str, Option<&'a str>)>,
    system_locales: impl IntoIterator<Item = &'a str>,
) -> String {
    let env_values = env_values.into_iter().collect::<Vec<_>>();
    for name in LANGUAGE_ENV_VARS {
        let value = env_values
            .iter()
            .find_map(|(candidate_name, value)| (*candidate_name == *name).then_some(value))
            .copied()
            .flatten();
        if let Some(language) = value.and_then(language_code_from_locale) {
            return language;
        }
    }

    for locale in system_locales {
        if let Some(language) = language_code_from_locale(locale) {
            return language;
        }
    }

    DEFAULT_LANGUAGE.to_owned()
}

fn language_code_from_locale(locale: &str) -> Option<String> {
    let locale = locale.trim();
    if locale.is_empty() {
        return None;
    }
    let candidate = locale
        .split(':')
        .find(|part| !part.trim().is_empty())
        .unwrap_or(locale)
        .trim();
    let language = candidate
        .split(['_', '-', '.'])
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    SUPPORTED_LANGUAGES
        .contains(&language.as_str())
        .then_some(language)
}

#[cfg(target_os = "macos")]
fn system_locale_candidates() -> Vec<String> {
    static SYSTEM_LOCALES: OnceLock<Vec<String>> = OnceLock::new();
    SYSTEM_LOCALES
        .get_or_init(|| {
            let mut candidates = Vec::new();
            candidates.extend(defaults_read_tokens(&["read", "-g", "AppleLocale"]));
            candidates.extend(defaults_read_tokens(&["read", "-g", "AppleLanguages"]));
            candidates
        })
        .clone()
}

#[cfg(not(target_os = "macos"))]
fn system_locale_candidates() -> Vec<String> {
    Vec::new()
}

#[cfg(target_os = "macos")]
fn defaults_read_tokens(args: &[&str]) -> Vec<String> {
    let Ok(output) = std::process::Command::new("defaults").args(args).output() else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&output.stdout);
    text.split(|ch: char| !(ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.')))
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .collect()
}
