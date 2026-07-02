use std::fs;

use iac_code_protocol::permission::PermissionMode;

use crate::permission_settings::{
    load_tool_permission_context, parse_permission_settings, session_trusted_read_directories,
};
use crate::test_support::{paths_for, unique_temp_dir};

#[test]
fn permission_context_merges_settings_layers_and_cli_rules_like_python() {
    let root = unique_temp_dir("iac-code-rs-permission-context");
    let config_dir = root.join("config");
    let cwd = root.join("workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(cwd.join(".iac-code")).expect("project settings dir should be created");
    let paths = paths_for(&config_dir);

    fs::write(
            &paths.settings_path,
            "permissions:\n  allow:\n    - bash(git *)\n  mode: default\n  additional_directories:\n    - /shared\n",
        )
        .expect("global settings should be written");
    fs::write(
        cwd.join(".iac-code").join("settings.yml"),
        "permissions:\n  deny:\n    - read_file\n  mode: dont_ask\n",
    )
    .expect("project settings should be written");
    fs::write(
        cwd.join(".iac-code").join("settings.local.yml"),
        "permissions:\n  ask:\n    - bash\n  mode: bypass_permissions\n",
    )
    .expect("local settings should be written");

    let context = load_tool_permission_context(
        &paths,
        "write_file",
        "edit_file, read_file",
        "accept_edits",
        &cwd.to_string_lossy(),
    )
    .expect("permission context should load");

    assert_eq!(context.mode, PermissionMode::AcceptEdits);
    assert_eq!(
        context.allow_rules.get("user_settings"),
        Some(&vec!["bash(git *)".to_string()])
    );
    assert_eq!(
        context.allow_rules.get("cli_arg"),
        Some(&vec!["write_file".to_string()])
    );
    assert_eq!(
        context.deny_rules.get("project_settings"),
        Some(&vec!["read_file".to_string()])
    );
    assert_eq!(
        context.deny_rules.get("cli_arg"),
        Some(&vec!["edit_file".to_string(), "read_file".to_string()])
    );
    assert_eq!(
        context.ask_rules.get("local_settings"),
        Some(&vec!["bash".to_string()])
    );
    assert_eq!(context.additional_directories, vec!["/shared".to_string()]);
    assert!(context.trusted_read_directories.is_empty());

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_trusted_read_directories_match_python_roots_and_validation() {
    let root = unique_temp_dir("iac-code-rs-session-trusted-roots");
    let config_dir = root.join("config");
    let paths = paths_for(&config_dir);

    assert_eq!(
        session_trusted_read_directories(&paths, Some("session-42"))
            .expect("session roots should build"),
        vec![
            config_dir
                .join("tool-results")
                .join("session-42")
                .to_string_lossy()
                .into_owned(),
            config_dir
                .join("image-cache")
                .join("session-42")
                .to_string_lossy()
                .into_owned(),
        ]
    );
    assert!(session_trusted_read_directories(&paths, None)
        .expect("missing session should be accepted")
        .is_empty());
    assert!(session_trusted_read_directories(&paths, Some(""))
        .expect("empty session should be accepted")
        .is_empty());
    for invalid in [".", "..", "a/b", "a\\b"] {
        assert!(
            session_trusted_read_directories(&paths, Some(invalid)).is_err(),
            "{invalid} should be rejected"
        );
    }

    fs::remove_dir_all(root).ok();
}

#[test]
fn permission_inline_lists_keep_commas_inside_quoted_rules_like_python_yaml() {
    let settings = parse_permission_settings(
            "permissions:\n  allow: [\"bash(echo a,b)\", read_file]\n  additional_directories: [\"/tmp/with,comma\"]\n",
        );

    assert_eq!(
        settings.allow,
        vec!["bash(echo a,b)".to_string(), "read_file".to_string()]
    );
    assert_eq!(
        settings.additional_directories,
        vec!["/tmp/with,comma".to_string()]
    );
}

#[test]
fn permission_settings_coerce_yaml_list_values_like_python_safe_load() {
    let settings = parse_permission_settings(
            "permissions:\n  allow:\n    - read_file\n    - 7\n    - true\n    - null\n  deny: [false, edit_file]\n  additional_directories:\n    - /shared\n    - null\n",
        );

    assert_eq!(
        settings.allow,
        vec!["read_file".to_string(), "7".to_string(), "True".to_string(),]
    );
    assert_eq!(
        settings.deny,
        vec!["False".to_string(), "edit_file".to_string()]
    );
    assert_eq!(settings.additional_directories, vec!["/shared".to_string()]);
}
