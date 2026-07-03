use std::collections::BTreeMap;
use std::fs;

use crate::a2a_config_yaml::parse_a2a_client_config_content;

mod options;

use options::{
    append_a2a_auth_config_options, append_a2a_card_config_options, append_config_bool_flag,
    append_config_option, append_config_option_for_first_key, append_route_config_option,
};

pub(super) type A2AClientConfig = BTreeMap<String, String>;

pub(super) fn load_a2a_client_config(path: &str) -> Result<A2AClientConfig, String> {
    if path.is_empty() {
        return Ok(A2AClientConfig::new());
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    parse_a2a_client_config(&content)
}

pub(super) fn parse_a2a_client_config(content: &str) -> Result<A2AClientConfig, String> {
    parse_a2a_client_config_content(content)
}

pub(super) fn apply_a2a_client_config(
    command: &str,
    mut args: Vec<String>,
    config: &A2AClientConfig,
) -> Vec<String> {
    if config.is_empty() {
        return args;
    }

    match command {
        "call" => {
            append_config_option(&mut args, config, "url", "--url");
            append_route_config_option(&mut args, config);
            append_config_option(&mut args, config, "route_name", "--route-name");
            append_config_option(&mut args, config, "cwd", "--cwd");
            append_config_option(&mut args, config, "context_id", "--context-id");
            append_config_option(&mut args, config, "model", "--model");
            append_config_option(&mut args, config, "timeout", "--timeout");
            append_config_bool_flag(&mut args, config, "stream", "--stream");
            append_a2a_card_config_options(&mut args, config);
            append_a2a_auth_config_options(&mut args, config);
        }
        "discover" => {
            append_config_option(&mut args, config, "url", "--url");
            append_a2a_card_config_options(&mut args, config);
            append_a2a_auth_config_options(&mut args, config);
        }
        "task-get" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "task_id", "--task-id");
            append_config_option(&mut args, config, "history_length", "--history-length");
            append_a2a_auth_config_options(&mut args, config);
        }
        "task-list" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "context_id", "--context-id");
            append_config_option(&mut args, config, "status", "--status");
            append_config_option(&mut args, config, "page_size", "--page-size");
            append_config_option(&mut args, config, "page_token", "--page-token");
            append_config_bool_flag(
                &mut args,
                config,
                "include_artifacts",
                "--include-artifacts",
            );
            append_config_option(&mut args, config, "output", "--output");
            append_a2a_auth_config_options(&mut args, config);
        }
        "task-cancel" | "task-subscribe" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "task_id", "--task-id");
            append_a2a_auth_config_options(&mut args, config);
        }
        "push-config-create" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "task_id", "--task-id");
            append_config_option(&mut args, config, "config_id", "--config-id");
            append_config_option(&mut args, config, "callback_url", "--callback-url");
            append_config_option(
                &mut args,
                config,
                "notification_token",
                "--notification-token",
            );
            append_config_option(&mut args, config, "auth_scheme", "--auth-scheme");
            append_config_option(&mut args, config, "auth_credentials", "--auth-credentials");
            append_a2a_auth_config_options(&mut args, config);
        }
        "push-config-get" | "push-config-delete" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "task_id", "--task-id");
            append_config_option(&mut args, config, "config_id", "--config-id");
            append_a2a_auth_config_options(&mut args, config);
        }
        "push-config-list" => {
            append_config_option(&mut args, config, "url", "--url");
            append_config_option(&mut args, config, "task_id", "--task-id");
            append_config_option(&mut args, config, "page_size", "--page-size");
            append_config_option(&mut args, config, "page_token", "--page-token");
            append_a2a_auth_config_options(&mut args, config);
        }
        "extended-card" => {
            append_config_option(&mut args, config, "url", "--url");
            append_a2a_auth_config_options(&mut args, config);
        }
        "route-preview" => {
            append_route_config_option(&mut args, config);
            append_config_option_for_first_key(
                &mut args,
                config,
                &["name", "route_name"],
                "--name",
                &["--name", "--route-name"],
            );
            append_config_option(&mut args, config, "skill", "--skill");
            append_config_option(&mut args, config, "prompt", "--prompt");
            append_config_option_for_first_key(
                &mut args,
                config,
                &["route_state_dir", "persistence_dir"],
                "--route-state-dir",
                &["--route-state-dir", "--persistence-dir"],
            );
            append_config_bool_flag(&mut args, config, "save_routes", "--save-routes");
        }
        _ => {}
    }
    args
}

pub(super) fn yaml_bool_value(value: &str) -> bool {
    options::yaml_bool_value(value)
}

pub(super) fn config_values<'a>(
    config: &'a A2AClientConfig,
    key: &str,
) -> impl Iterator<Item = &'a str> + 'a {
    options::config_values(config, key)
}

pub(super) fn apply_config_string(target: &mut String, config: &A2AClientConfig, key: &str) {
    options::apply_config_string(target, config, key);
}

pub(super) fn apply_config_u16(target: &mut u16, config: &A2AClientConfig, key: &str) {
    options::apply_config_u16(target, config, key);
}

pub(super) fn apply_config_u64(target: &mut u64, config: &A2AClientConfig, key: &str) {
    options::apply_config_u64(target, config, key);
}

pub(super) fn apply_config_optional_u16(
    target: &mut Option<u16>,
    config: &A2AClientConfig,
    key: &str,
) {
    options::apply_config_optional_u16(target, config, key);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a2a_client_parent_config_preserves_yaml_route_lists_like_python() {
        let config = parse_a2a_client_config(
            "route:\n  - template=http://template;skills=iac_generation;tags=ros\n  - review=http://review;skills=iac_review;tags=review\nroute-name: review\n",
        )
        .expect("valid route list config");

        let args = apply_a2a_client_config(
            "call",
            vec!["--prompt".to_owned(), "review ros".to_owned()],
            &config,
        );
        let routes = args
            .windows(2)
            .filter_map(|window| (window[0] == "--route").then_some(window[1].as_str()))
            .collect::<Vec<_>>();

        assert_eq!(
            routes,
            vec![
                "template=http://template;skills=iac_generation;tags=ros",
                "review=http://review;skills=iac_review;tags=review",
            ]
        );
        assert!(args
            .windows(2)
            .any(|window| window == ["--route-name", "review"]));
    }

    #[test]
    fn a2a_client_parent_config_applies_call_model_like_python() {
        let config = parse_a2a_client_config("url: http://agent.example/rpc\nmodel: qwen3.7-max\n")
            .expect("valid a2a client config");

        let args = apply_a2a_client_config(
            "call",
            vec!["--prompt".to_owned(), "create vpc".to_owned()],
            &config,
        );

        assert!(args
            .windows(2)
            .any(|window| window == ["--model", "qwen3.7-max"]));
    }

    #[test]
    fn a2a_client_parent_config_accepts_yaml_route_mappings_like_python() {
        let config = parse_a2a_client_config(
            "route-name: template\nroutes:\n  - name: template\n    url: http://template.example/rpc\n    skills:\n      - iac_generation\n    tags:\n      - ros\n      - template\n  - name: review\n    url: http://review.example/rpc\n    skills:\n      - iac_review\n",
        )
        .expect("valid route mapping config");

        let args = apply_a2a_client_config(
            "call",
            vec!["--prompt".to_owned(), "create vpc".to_owned()],
            &config,
        );
        let routes = args
            .windows(2)
            .filter_map(|window| (window[0] == "--route").then_some(window[1].as_str()))
            .collect::<Vec<_>>();

        assert_eq!(
            routes,
            vec![
                "template=http://template.example/rpc;skills=iac_generation;tags=ros,template",
                "review=http://review.example/rpc;skills=iac_review",
            ]
        );
        assert!(args
            .windows(2)
            .any(|window| window == ["--route-name", "template"]));
    }

    #[test]
    fn a2a_client_config_rejects_non_mapping_yaml_like_python() {
        let error = parse_a2a_client_config("- not-a-mapping\n").expect_err("non-mapping config");

        assert_eq!(error, "A2A config file must contain a YAML mapping.");
    }

    #[test]
    fn a2a_client_config_rejects_invalid_route_mapping_entries_like_python() {
        let error = parse_a2a_client_config(
            "routes:\n  - name: template\n    skills:\n      - iac_generation\n",
        )
        .expect_err("route mapping without url");

        assert_eq!(
            error,
            "A2A client route config entries require name and url."
        );
    }
}
