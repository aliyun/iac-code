use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_config::cloud_credentials::{save_aliyun_credentials, AliyunCredential};
use iac_code_config::credentials::{load_credentials, save_llm_key};
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{
    get_active_provider_key, get_llm_source, get_provider_config, load_active_provider_config,
    load_active_provider_effort, load_disabled_skills, load_saved_effort, load_saved_model,
    save_active_provider_config, save_active_provider_effort, save_active_provider_model,
    save_disabled_skills, save_llm_source, save_saved_effort, PROVIDER_KEYS,
};
use iac_code_protocol::json::{self, JsonValue};
use iac_code_providers::provider_keys;

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn config_helpers_match_python_fixture() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");

    let actual = capture_rust_config_fixture(&parent);
    let expected = compact_json(&fixture_text());

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();

    assert_eq!(actual.to_compact_json(), expected);
}

#[test]
fn disabled_skill_settings_match_python_rules() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "disabled-skills");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");
    fs::write(
        &paths.settings_path,
        "activeProvider: dashscope\ndisabled_skills:\n  - \" Demo \"\n  - ''\n  - 7\n  - Other\nproviders:\n  dashscope:\n    model: saved-model\n",
    )
    .expect("settings should be written");

    assert_eq!(
        load_disabled_skills(&paths)
            .expect("disabled skills should load")
            .into_iter()
            .collect::<Vec<_>>(),
        vec!["demo".to_owned(), "other".to_owned()]
    );

    save_disabled_skills(&paths, ["Beta", "alpha", "iac-aliyun"], ["iac-aliyun"])
        .expect("disabled skills should save");

    let saved = fs::read_to_string(&paths.settings_path).expect("settings should be readable");
    assert!(saved.contains("activeProvider: dashscope"));
    assert!(saved.contains("providers:\n  dashscope:\n    model: saved-model"));
    assert_eq!(
        load_disabled_skills(&paths)
            .expect("disabled skills should reload")
            .into_iter()
            .collect::<Vec<_>>(),
        vec!["alpha".to_owned(), "beta".to_owned()]
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[test]
fn model_and_effort_settings_save_preserves_existing_sections() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "model-effort");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");
    fs::write(
        &paths.settings_path,
        "activeProvider: openai\ndisabled_skills:\n- user-helper\nproviders:\n  openai:\n    apiBase: https://openai.invalid/v1\n    model: gpt-5.4\n  dashscope:\n    model: qwen3.7-max\n",
    )
    .expect("settings should be written");

    save_active_provider_model(&paths, "gpt-5.5").expect("model should save");
    save_saved_effort(&paths, "low").expect("effort should save");

    let saved = fs::read_to_string(&paths.settings_path).expect("settings should be readable");
    assert!(saved.contains("activeProvider: openai"));
    assert!(saved.contains("disabled_skills:\n- user-helper"));
    assert!(saved.contains("openai:\n    apiBase: https://openai.invalid/v1\n    model: gpt-5.5"));
    assert!(saved.contains("dashscope:\n    model: qwen3.7-max"));
    assert_eq!(
        load_saved_model(&paths).expect("model should reload"),
        Some("gpt-5.5".to_owned())
    );
    assert_eq!(
        load_saved_effort(&paths).expect("effort should reload"),
        Some("low".to_owned())
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[test]
fn active_provider_effort_uses_provider_config_like_python_effort_command() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "provider-effort");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");
    fs::write(
        &paths.settings_path,
        "activeProvider: openai\neffort: xhigh\nproviders:\n  openai:\n    effort: medium\n    model: gpt-5.4\n  dashscope:\n    effort: low\n    model: qwen3.7-max\n",
    )
    .expect("settings should be written");

    assert_eq!(
        load_saved_effort(&paths).expect("legacy root effort should load"),
        Some("xhigh".to_owned())
    );
    assert_eq!(
        load_active_provider_effort(&paths).expect("active provider effort should load"),
        Some("medium".to_owned())
    );

    save_active_provider_effort(&paths, "high").expect("active provider effort should save");

    let saved = fs::read_to_string(&paths.settings_path).expect("settings should be readable");
    assert!(saved.contains("effort: xhigh"), "{saved}");
    assert!(
        saved.contains("openai:\n    effort: high\n    model: gpt-5.4"),
        "{saved}"
    );
    assert!(
        saved.contains("dashscope:\n    effort: low\n    model: qwen3.7-max"),
        "{saved}"
    );
    assert_eq!(
        load_active_provider_effort(&paths).expect("active provider effort should reload"),
        Some("high".to_owned())
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[test]
fn save_llm_source_removes_active_provider_like_python_auth_partner_flow() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "llm-source");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");
    fs::write(
        &paths.settings_path,
        "activeProvider: dashscope\nproviders:\n  dashscope:\n    model: qwen3.7-max\n",
    )
    .expect("settings should be written");

    save_llm_source(&paths, "qwenpaw").expect("llm source should save");

    let settings = fs::read_to_string(&paths.settings_path).expect("settings should be readable");
    assert!(!settings.contains("activeProvider"), "{settings}");
    assert!(settings.contains("llm_source: qwenpaw"), "{settings}");
    assert!(settings.contains("providers:"), "{settings}");
    assert_eq!(
        get_llm_source(&paths).expect("llm source should load"),
        "qwenpaw"
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[cfg(unix)]
#[test]
fn config_dir_env_resolves_existing_symlink_ancestors_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    let real = parent.join("real-config-root");
    let link = parent.join("config-link");
    fs::create_dir_all(&real).expect("real config root should be created");
    std::os::unix::fs::symlink(&real, &link).expect("config symlink should be created");
    std::env::set_var("IAC_CODE_CONFIG_DIR", link.join("nested"));

    let paths = ConfigPaths::from_env().expect("paths should resolve");

    assert_eq!(
        paths.config_dir,
        real.canonicalize()
            .expect("real config root should canonicalize")
            .join("nested")
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[test]
fn auth_storage_saves_credentials_and_active_provider_config() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "auth-storage");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");

    save_llm_key(&paths, "openai", "sk-test").expect("credential should save");
    save_active_provider_config(&paths, "openai", "gpt-5.5", None)
        .expect("provider config should save");

    assert_eq!(
        load_credentials(&paths, Some("gpt-5.5"))
            .expect("credentials should load")
            .get("openai")
            .cloned(),
        Some("sk-test".to_owned())
    );
    assert_eq!(
        get_active_provider_key(&paths).expect("active provider should load"),
        Some("openai".to_owned())
    );
    assert_eq!(
        load_saved_model(&paths).expect("model should load"),
        Some("gpt-5.5".to_owned())
    );
    let saved = fs::read_to_string(&paths.settings_path).expect("settings should be readable");
    assert!(saved.contains("name: OpenAI"), "{saved}");

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[test]
fn openai_compatible_provider_alias_maps_to_openapi_compatible_slot() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "openai-compatible-alias");
    fs::create_dir_all(&paths.config_dir).expect("config dir should be created");
    fs::write(
        &paths.settings_path,
        "activeProvider: openai_compatible\nproviders:\n  openai_compatible:\n    apiBase: https://compat.invalid/v1\n    model: compat-model\n",
    )
    .expect("settings should be written");
    fs::write(&paths.credentials_path, "openai_compatible: compat-key\n")
        .expect("credentials should be written");

    assert_eq!(
        get_active_provider_key(&paths).expect("active provider should load"),
        Some("openapi_compatible".to_owned())
    );
    assert_eq!(
        get_provider_config(&paths, "openapi_compatible")
            .expect("provider config should load")
            .get("apiBase")
            .cloned(),
        Some("https://compat.invalid/v1".to_owned())
    );
    assert_eq!(
        load_credentials(&paths, None)
            .expect("credentials should load")
            .get("openapi_compatible")
            .cloned(),
        Some("compat-key".to_owned())
    );

    std::env::set_var("IAC_CODE_PROVIDER", "OpenAI Compatible");
    assert_eq!(
        get_active_provider_key(&paths).expect("env provider should load"),
        Some("openapi_compatible".to_owned())
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

#[cfg(unix)]
#[test]
fn config_writes_private_settings_and_credentials_like_python() {
    use std::os::unix::fs::PermissionsExt;

    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "private-writes");

    save_llm_key(&paths, "openai", "sk-test").expect("credential should save");
    save_active_provider_config(&paths, "openai", "gpt-5.5", None)
        .expect("provider config should save");
    save_aliyun_credentials(
        &paths.cloud_credentials_path,
        &AliyunCredential {
            access_key_id: "ak-test".to_owned(),
            access_key_secret: "secret-test".to_owned(),
            ..AliyunCredential::default()
        },
    )
    .expect("cloud credential should save");

    assert_eq!(
        fs::metadata(&paths.config_dir)
            .expect("config dir metadata")
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&paths.credentials_path)
            .expect("credentials metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&paths.settings_path)
            .expect("settings metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&paths.cloud_credentials_path)
            .expect("cloud credentials metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

fn capture_rust_config_fixture(parent: &Path) -> JsonValue {
    let raw_config_dir = "$IAC_CODE_RS_CONFIG_PARENT/nested/../config";
    std::env::set_var("IAC_CODE_RS_CONFIG_PARENT", parent);
    std::env::set_var("IAC_CODE_CONFIG_DIR", raw_config_dir);

    let paths = ConfigPaths::from_env().expect("paths should resolve");
    write_base_settings(&paths.settings_path);
    write_base_credentials(&paths.credentials_path);

    let base_case = json::object([
        ("raw_config_dir", json::string(raw_config_dir)),
        (
            "config_dir",
            json::string(normalize_path(&paths.config_dir, parent)),
        ),
        (
            "settings_path",
            json::string(normalize_path(&paths.settings_path, parent)),
        ),
        (
            "credentials_path",
            json::string(normalize_path(&paths.credentials_path, parent)),
        ),
        (
            "cloud_credentials_path",
            json::string(normalize_path(&paths.cloud_credentials_path, parent)),
        ),
        (
            "history_path",
            json::string(normalize_path(&paths.history_path, parent)),
        ),
        (
            "projects_dir",
            json::string(normalize_path(&paths.subdirs().projects, parent)),
        ),
        ("subdirs", subdirs_json(&paths, parent)),
        (
            "active_provider",
            option_string(get_active_provider_key(&paths).expect("active provider should load")),
        ),
        (
            "provider_dashscope",
            string_map(
                &get_provider_config(&paths, "dashscope").expect("provider config should load"),
            ),
        ),
        (
            "saved_model",
            option_string(load_saved_model(&paths).expect("saved model should load")),
        ),
        (
            "saved_effort",
            option_string(load_saved_effort(&paths).expect("saved effort should load")),
        ),
        (
            "active_provider_config",
            option_string_map(
                load_active_provider_config(&paths).expect("active provider config should load"),
            ),
        ),
        (
            "llm_source",
            json::string(get_llm_source(&paths).expect("llm source should load")),
        ),
        (
            "credential_slots",
            string_map(&load_credentials(&paths, None).expect("credentials should load")),
        ),
    ]);

    std::env::set_var("IAC_CODE_PROVIDER", "OpenAPI Compatible");
    std::env::set_var("IAC_CODE_MODEL", "env-openapi-model");
    std::env::set_var("IAC_CODE_BASE_URL", "https://env-openapi.invalid/v1");
    std::env::set_var("IAC_CODE_API_KEY", "fixture-env-openapi-value");

    let env_openapi_case = json::object([
        (
            "active_provider",
            option_string(
                get_active_provider_key(&paths).expect("env active provider should load"),
            ),
        ),
        (
            "provider_openapi",
            string_map(
                &get_provider_config(&paths, "openapi_compatible")
                    .expect("openapi config should load"),
            ),
        ),
        (
            "credential_slots",
            string_map(&load_credentials(&paths, None).expect("env credentials should load")),
        ),
        (
            "llm_source",
            json::string(get_llm_source(&paths).expect("env llm source should load")),
        ),
    ]);

    clear_iac_env();
    let model_inference_paths = paths_for(parent, "model-inference");
    write_model_inference_credentials(&model_inference_paths.credentials_path);
    std::env::set_var("IAC_CODE_API_KEY", "fixture-env-anthropic-value");
    let model_inference_case = json::object([
        (
            "credential_slots",
            string_map(
                &load_credentials(&model_inference_paths, Some("claude-opus-4-7"))
                    .expect("model inference credentials should load"),
            ),
        ),
        (
            "llm_source",
            json::string(
                get_llm_source(&model_inference_paths).expect("model inference source should load"),
            ),
        ),
    ]);

    clear_iac_env();
    let partner_paths = paths_for(parent, "partner-source");
    fs::write(&partner_paths.settings_path, "llm_source: qwenpaw\n")
        .expect("partner settings should be written");
    let partner_source = get_llm_source(&partner_paths).expect("partner source should load");

    clear_iac_env();
    let empty_paths = paths_for(parent, "empty-config");
    let default_source = get_llm_source(&empty_paths).expect("default source should load");

    json::object([
        (
            "provider_keys",
            json::array(PROVIDER_KEYS.iter().map(|key| json::string(*key))),
        ),
        ("base_case", base_case),
        ("env_openapi_case", env_openapi_case),
        ("model_inference_case", model_inference_case),
        (
            "llm_source_cases",
            json::object([
                (
                    "partner_without_active_provider",
                    json::string(partner_source),
                ),
                ("default_without_settings", json::string(default_source)),
            ]),
        ),
    ])
}

#[test]
fn provider_key_list_matches_provider_registry() {
    assert_eq!(PROVIDER_KEYS, provider_keys());
}

#[test]
fn invalid_iac_code_provider_error_matches_python_config() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_iac_env();

    let parent = unique_temp_dir();
    fs::create_dir_all(&parent).expect("temp parent should be created");
    let paths = paths_for(&parent, "invalid-provider-env");
    std::env::set_var("IAC_CODE_PROVIDER", "Nope");

    let error = get_active_provider_key(&paths)
        .expect_err("invalid provider env should fail")
        .to_string();

    assert_eq!(
        error,
        "Invalid IAC_CODE_PROVIDER value: 'Nope'. Valid values (case-insensitive): DashScope, \
DashScope Token Plan, OpenAI, Anthropic, DeepSeek, OpenAPI Compatible, Anthropic Compatible, \
Gemini, Kimi CN, Kimi Intl, MiniMax CN, MiniMax Intl, ZhiPu CN, ZhiPu Intl, Volcengine CN, \
SiliconFlow CN, SiliconFlow Intl, Ollama, LM Studio, OpenRouter, Azure OpenAI, ModelScope, \
Aliyun CodingPlan, Aliyun CodingPlan Intl, ZhiPu CN CodingPlan, ZhiPu Intl CodingPlan, \
Volcengine CodingPlan"
    );

    fs::remove_dir_all(&parent).ok();
    clear_iac_env();
}

fn write_base_settings(path: &Path) {
    fs::write(
        path,
        r#"activeProvider: bailian
effort: high
llm_source: qwenpaw
providers:
  bailian:
    apiBase: https://legacy.invalid/v1
    model: qwen3.6-plus
  openai:
    model: gpt-5.4
  openapi_compatible:
    apiBase: https://saved.invalid/v1
    model: saved-openapi-model
"#,
    )
    .expect("base settings should be written");
}

fn write_base_credentials(path: &Path) {
    fs::write(
        path,
        r#"anthropic: fixture-anthropic-value
bailian: fixture-legacy-dashscope-value
dashscope: fixture-dashscope-value
deepseek: fixture-deepseek-value
openai: fixture-openai-value
openapi_compatible: fixture-openapi-compatible-value
"#,
    )
    .expect("base credentials should be written");
}

fn write_model_inference_credentials(path: &Path) {
    fs::write(
        path,
        r#"anthropic: fixture-anthropic-value
openai: fixture-openai-value
"#,
    )
    .expect("model inference credentials should be written");
}

fn paths_for(parent: &Path, name: &str) -> ConfigPaths {
    std::env::set_var("IAC_CODE_CONFIG_DIR", parent.join(name));
    ConfigPaths::from_env().expect("case paths should resolve")
}

fn subdirs_json(paths: &ConfigPaths, parent: &Path) -> JsonValue {
    let subdirs = paths.subdirs();
    json::object([
        (
            "projects",
            json::string(normalize_path(&subdirs.projects, parent)),
        ),
        (
            "image-cache",
            json::string(normalize_path(&subdirs.image_cache, parent)),
        ),
        (
            "tool-results",
            json::string(normalize_path(&subdirs.tool_results, parent)),
        ),
        ("logs", json::string(normalize_path(&subdirs.logs, parent))),
        (
            "memory",
            json::string(normalize_path(&subdirs.memory, parent)),
        ),
        ("a2a", json::string(normalize_path(&subdirs.a2a, parent))),
        (
            "telemetry",
            json::string(normalize_path(&subdirs.telemetry, parent)),
        ),
        (
            "skills",
            json::string(normalize_path(&subdirs.skills, parent)),
        ),
    ])
}

fn string_map(values: &BTreeMap<String, String>) -> JsonValue {
    json::object(
        values
            .iter()
            .map(|(key, value)| (key.as_str(), json::string(value.as_str()))),
    )
}

fn option_string_map(value: Option<BTreeMap<String, String>>) -> JsonValue {
    value.map_or_else(json::null, |values| string_map(&values))
}

fn option_string(value: Option<String>) -> JsonValue {
    value.map_or_else(json::null, json::string)
}

fn normalize_path(path: &Path, parent: &Path) -> String {
    let raw_parent = parent.to_string_lossy().to_string();
    let canonical_parent = parent
        .canonicalize()
        .ok()
        .map(|path| path.to_string_lossy().to_string());
    let mut value = path.to_string_lossy().to_string();
    if let Some(canonical_parent) = canonical_parent {
        value = value.replace(&canonical_parent, "$CONFIG_PARENT");
    }
    value.replace(&raw_parent, "$CONFIG_PARENT")
}

fn compact_json(input: &str) -> String {
    let mut output = String::new();
    let mut in_string = false;
    let mut escaped = false;

    for character in input.chars() {
        if in_string {
            output.push(character);
            if escaped {
                escaped = false;
            } else if character == '\\' {
                escaped = true;
            } else if character == '"' {
                in_string = false;
            }
            continue;
        }

        if character == '"' {
            in_string = true;
            output.push(character);
        } else if !character.is_whitespace() {
            output.push(character);
        }
    }

    output
}

fn fixture_text() -> String {
    let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    path.push("../../fixtures/compatibility/config_basic/config.json");
    fs::read_to_string(path).expect("config fixture should be readable")
}

fn unique_temp_dir() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("iac-code-rs-config-{}-{nanos}", std::process::id()))
}

fn clear_iac_env() {
    for key in [
        "IAC_CODE_CONFIG_DIR",
        "IAC_CODE_PROVIDER",
        "IAC_CODE_MODEL",
        "IAC_CODE_BASE_URL",
        "IAC_CODE_API_KEY",
        "IAC_CODE_RS_CONFIG_PARENT",
    ] {
        std::env::remove_var(key);
    }
}
