use iac_code_config::i18n::{detect_language_from_sources, DEFAULT_LANGUAGE};

#[test]
fn detect_language_matches_python_environment_priority() {
    let env_values = [
        ("LANGUAGE", Some("fr_FR.UTF-8")),
        ("LC_ALL", Some("zh_CN.UTF-8")),
        ("LC_MESSAGES", Some("ja_JP.UTF-8")),
        ("LANG", Some("de_DE.UTF-8")),
    ];

    assert_eq!(
        detect_language_from_sources(env_values, std::iter::empty::<&str>()),
        "fr"
    );
}

#[test]
fn detect_language_falls_back_to_system_locale_when_env_is_unset() {
    let env_values = [
        ("LANGUAGE", None),
        ("LC_ALL", None),
        ("LC_MESSAGES", None),
        ("LANG", None),
    ];

    assert_eq!(
        detect_language_from_sources(env_values, ["zh-Hans-CN", "en-US"]),
        "zh"
    );
}

#[test]
fn detect_language_uses_english_for_unsupported_locales() {
    let env_values = [
        ("LANGUAGE", Some("ru_RU.UTF-8")),
        ("LC_ALL", None),
        ("LC_MESSAGES", None),
        ("LANG", None),
    ];

    assert_eq!(
        detect_language_from_sources(env_values, ["ko-KR"]),
        DEFAULT_LANGUAGE
    );
}
