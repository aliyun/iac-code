use std::fs;
#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::path::PathBuf;
#[cfg(unix)]
use std::thread::{self, JoinHandle};

#[cfg(unix)]
use iac_code_config::paths::ConfigPaths;
#[cfg(unix)]
use iac_code_exec::EXIT_OK;
#[cfg(unix)]
use iac_code_tools::TaskManager;

use crate::interactive_session::{raw_prompt_action_context, InteractiveSessionState};
use crate::raw_auth_cloud::{
    raw_auth_aliyun_credential_mode, raw_auth_aliyun_existing_credential_action,
    raw_auth_aliyun_option_choice, raw_auth_cloud_provider_choice,
    RawAuthAliyunExistingCredentialAction, RawAuthAliyunOptionChoice, RawAuthCloudProviderChoice,
};
use crate::raw_prompt_input::{
    read_raw_interactive_prompt_input_with_context_and_image_source, RawInteractivePromptInput,
};
use crate::test_support::{
    assert_bytes_contains, english_locale_guard, paths_for, raw_prompt_render, read_fd_exact,
    read_fd_until_contains, terminal_mode_bytes, unique_temp_dir, write_fd, EnvVarGuard,
    PseudoTerminal, StaticRawPromptImageSource,
};

#[cfg(unix)]
fn spawn_raw_auth_prompt_reader(
    slave: RawFd,
    root: PathBuf,
    paths: ConfigPaths,
) -> JoinHandle<Option<RawInteractivePromptInput>> {
    thread::spawn(move || {
        let root_text = root.to_string_lossy().into_owned();
        let state = InteractiveSessionState {
            resume: String::new(),
            continue_session: false,
            exit_code: EXIT_OK,
            turn_count: 0,
            token_count: 0,
            debug_enabled: false,
            debug_log_path: None,
            current_session_id: None,
            task_manager: TaskManager::new(),
            input_history: None,
            transcript_lines: Vec::new(),
        };
        let context = raw_prompt_action_context(Some(&paths), &root_text, &state);
        let mut image_source = StaticRawPromptImageSource::new(vec![]);
        read_raw_interactive_prompt_input_with_context_and_image_source(
            slave,
            None,
            &root,
            vec![],
            &context,
            &mut image_source,
            &mut Vec::new(),
        )
        .expect("raw prompt should read")
    })
}

#[cfg(unix)]
#[test]
fn raw_auth_cloud_provider_choice_rejects_unknown_indexes() {
    assert_eq!(
        raw_auth_cloud_provider_choice(0),
        Some(RawAuthCloudProviderChoice::AlibabaCloud)
    );
    assert_eq!(raw_auth_cloud_provider_choice(1), None);
}

#[cfg(unix)]
#[test]
fn raw_auth_aliyun_option_choice_rejects_unknown_indexes() {
    assert_eq!(
        raw_auth_aliyun_option_choice(0),
        Some(RawAuthAliyunOptionChoice::Credential)
    );
    assert_eq!(
        raw_auth_aliyun_option_choice(1),
        Some(RawAuthAliyunOptionChoice::Region)
    );
    assert_eq!(raw_auth_aliyun_option_choice(2), None);
}

#[cfg(unix)]
#[test]
fn raw_auth_aliyun_existing_credential_action_rejects_unknown_indexes() {
    assert_eq!(
        raw_auth_aliyun_existing_credential_action(0),
        Some(RawAuthAliyunExistingCredentialAction::Reconfigure)
    );
    assert_eq!(
        raw_auth_aliyun_existing_credential_action(1),
        Some(RawAuthAliyunExistingCredentialAction::Back)
    );
    assert_eq!(raw_auth_aliyun_existing_credential_action(2), None);
}

#[cfg(unix)]
#[test]
fn raw_auth_aliyun_credential_mode_rejects_unknown_indexes() {
    assert_eq!(raw_auth_aliyun_credential_mode(0), Some("AK"));
    assert_eq!(raw_auth_aliyun_credential_mode(1), Some("StsToken"));
    assert_eq!(raw_auth_aliyun_credential_mode(2), Some("RamRoleArn"));
    assert_eq!(raw_auth_aliyun_credential_mode(3), Some("OAuth"));
    assert_eq!(raw_auth_aliyun_credential_mode(4), None);
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_saves_aliyun_ak_without_echoing_secret() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    let fake_access_key_id = "fake-cloud-ak";
    let fake_access_key_secret = "fake-cloud-secret";

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    assert_bytes_contains(&output, b"> Alibaba Cloud");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    assert_bytes_contains(&output, b"> Credential");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Select credential type",
    ));
    assert_bytes_contains(&output, b"> AccessKey");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey ID: "));
    write_fd(pty.master, fake_access_key_id.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"*************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey Secret: "));
    write_fd(pty.master, fake_access_key_secret.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"*****************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert!(
        !String::from_utf8_lossy(&output).contains(fake_access_key_id),
        "raw cloud auth output must not echo AccessKey ID"
    );
    assert!(
        !String::from_utf8_lossy(&output).contains(fake_access_key_secret),
        "raw cloud auth output must not echo AccessKey Secret"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let cloud_credentials = fs::read_to_string(config_dir.join(".cloud-credentials.yml"))
        .expect("cloud credentials should be written");
    assert!(cloud_credentials.contains("aliyun:"));
    assert!(cloud_credentials.contains("mode: AK"));
    assert!(cloud_credentials.contains("region_id: cn-hangzhou"));
    assert!(cloud_credentials.contains("access_key_id: fake-cloud-ak"));
    assert!(cloud_credentials.contains("access_key_secret: fake-cloud-secret"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_region_preserves_existing_aliyun_ak_secret() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-region-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-region-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
            config_dir.join(".cloud-credentials.yml"),
            "aliyun:\n  mode: AK\n  region_id: cn-hangzhou\n  access_key_id: fake-cloud-ak\n  access_key_secret: fake-cloud-secret\n",
        )
        .expect("cloud credentials should be written");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> Region"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Region: cn-hangzhou"));
    for _ in 0.."cn-hangzhou".len() {
        write_fd(pty.master, b"\x7f");
    }
    output.extend(read_fd_until_contains(pty.master, b"Region: "));
    write_fd(pty.master, b"cn-shanghai");
    output.extend(read_fd_until_contains(pty.master, b"Region: cn-shanghai"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert!(
        !String::from_utf8_lossy(&output).contains("fake-cloud-secret"),
        "raw cloud auth output must not echo existing AccessKey Secret"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let cloud_credentials = fs::read_to_string(config_dir.join(".cloud-credentials.yml"))
        .expect("cloud credentials should be written");
    assert!(cloud_credentials.contains("mode: AK"));
    assert!(cloud_credentials.contains("region_id: cn-shanghai"));
    assert!(cloud_credentials.contains("access_key_id: fake-cloud-ak"));
    assert!(cloud_credentials.contains("access_key_secret: fake-cloud-secret"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_saves_aliyun_sts_without_echoing_secrets() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-sts-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-sts-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    let fake_access_key_id = "fake-sts-ak";
    let fake_access_key_secret = "fake-sts-secret";
    let fake_sts_token = "fake-sts-token";

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Select credential type",
    ));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> STS Token"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey ID: "));
    write_fd(pty.master, fake_access_key_id.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"***********"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey Secret: "));
    write_fd(pty.master, fake_access_key_secret.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"***************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"STS Token: "));
    write_fd(pty.master, fake_sts_token.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"**************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    let output_text = String::from_utf8_lossy(&output);
    assert!(
        !output_text.contains(fake_access_key_id),
        "raw cloud auth output must not echo AccessKey ID"
    );
    assert!(
        !output_text.contains(fake_access_key_secret),
        "raw cloud auth output must not echo AccessKey Secret"
    );
    assert!(
        !output_text.contains(fake_sts_token),
        "raw cloud auth output must not echo STS token"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let cloud_credentials = fs::read_to_string(config_dir.join(".cloud-credentials.yml"))
        .expect("cloud credentials should be written");
    assert!(cloud_credentials.contains("mode: StsToken"));
    assert!(cloud_credentials.contains("region_id: cn-hangzhou"));
    assert!(cloud_credentials.contains("access_key_id: fake-sts-ak"));
    assert!(cloud_credentials.contains("access_key_secret: fake-sts-secret"));
    assert!(cloud_credentials.contains("sts_token: fake-sts-token"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_saves_aliyun_ram_role_without_echoing_secrets() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-ram-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-ram-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);
    let fake_access_key_id = "fake-ram-ak";
    let fake_access_key_secret = "fake-ram-secret";
    let ram_role_arn = "acs:ram::1234567890123456:role/iac-code-test";
    let ram_session_name = "iac-code-session";

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Select credential type",
    ));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> STS Token"));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> RAM Role"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey ID: "));
    write_fd(pty.master, fake_access_key_id.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"***********"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"AccessKey Secret: "));
    write_fd(pty.master, fake_access_key_secret.as_bytes());
    output.extend(read_fd_until_contains(pty.master, b"***************"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"RAM Role ARN: "));
    write_fd(pty.master, ram_role_arn.as_bytes());
    output.extend(read_fd_until_contains(pty.master, ram_role_arn.as_bytes()));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Session Name: "));
    write_fd(pty.master, ram_session_name.as_bytes());
    output.extend(read_fd_until_contains(
        pty.master,
        ram_session_name.as_bytes(),
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    let output_text = String::from_utf8_lossy(&output);
    assert!(
        !output_text.contains(fake_access_key_id),
        "raw cloud auth output must not echo AccessKey ID"
    );
    assert!(
        !output_text.contains(fake_access_key_secret),
        "raw cloud auth output must not echo AccessKey Secret"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let cloud_credentials = fs::read_to_string(config_dir.join(".cloud-credentials.yml"))
        .expect("cloud credentials should be written");
    assert!(cloud_credentials.contains("mode: RamRoleArn"));
    assert!(cloud_credentials.contains("region_id: cn-hangzhou"));
    assert!(cloud_credentials.contains("access_key_id: fake-ram-ak"));
    assert!(cloud_credentials.contains("access_key_secret: fake-ram-secret"));
    assert!(cloud_credentials
        .contains("ram_role_arn: \"acs:ram::1234567890123456:role/iac-code-test\""));
    assert!(cloud_credentials.contains("ram_session_name: iac-code-session"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_shows_existing_aliyun_oauth_config() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-oauth-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-oauth-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
            config_dir.join(".cloud-credentials.yml"),
            "aliyun:\n  mode: OAuth\n  region_id: cn-beijing\n  oauth_site_type: CN\n  oauth_access_token: fake-oauth-access\n  oauth_refresh_token: fake-oauth-refresh\n  oauth_access_token_expire: 1780479842\n  oauth_refresh_token_expire: 0\n  access_key_id: fake-oauth-ak\n  access_key_secret: fake-oauth-secret\n  sts_token: fake-oauth-sts\n  sts_expiration: 1780479844\n",
        )
        .expect("cloud credentials should be written");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Reconfigure credential",
    ));
    assert_bytes_contains(&output, b"Current configuration (iac-code)");
    assert_bytes_contains(&output, b"Mode: OAuth Login (Browser)");
    assert_bytes_contains(&output, b"OAuth Site Type: CN");
    assert_bytes_contains(&output, b"OAuth Access Token: *****************");
    assert_bytes_contains(&output, b"OAuth Refresh Token: ******************");
    assert_bytes_contains(&output, b"AccessKey ID: *************");
    assert_bytes_contains(&output, b"STS Token: **************");
    assert_bytes_contains(&output, b"Region: cn-beijing");
    assert!(
        !String::from_utf8_lossy(&output).contains("fake-oauth-access"),
        "raw cloud auth output must not echo OAuth access token"
    );

    write_fd(pty.master, b"\x1b");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_auth_cloud_oauth_login_saves_fake_browser_result() {
    let _env = EnvVarGuard::set_many(&[
            ("LANGUAGE", "en"),
            ("LC_ALL", "en_US.UTF-8"),
            ("LC_MESSAGES", "en_US.UTF-8"),
            ("LANG", "en_US.UTF-8"),
            (
                "IAC_CODE_RS_FAKE_ALIYUN_OAUTH_RESULT",
                "oauth_access_token=fake-oauth-access\noauth_refresh_token=fake-oauth-refresh\noauth_access_token_expire=1780479842\noauth_refresh_token_expire=0\naccess_key_id=fake-oauth-ak\naccess_key_secret=fake-oauth-secret\nsts_token=fake-oauth-sts\nsts_expiration=1780479844\n",
            ),
        ]);
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-oauth-login-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-auth-cloud-oauth-login-flow");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(&root).expect("root should exist");
    let paths = paths_for(&config_dir);

    let handle = spawn_raw_auth_prompt_reader(pty.slave, root.clone(), paths.clone());

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/auth\r");
    let mut output = read_fd_until_contains(pty.master, b"Select configuration type");
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> Configure IaC Cloud Service",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Select Cloud Provider"));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Configure Alibaba Cloud",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(
        pty.master,
        b"Select credential type",
    ));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> STS Token"));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(pty.master, b"> RAM Role"));
    write_fd(pty.master, b"\x1b[B");
    output.extend(read_fd_until_contains(
        pty.master,
        b"> OAuth Login (Browser)",
    ));
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, b"Choose site type"));
    assert_bytes_contains(&output, b"> China");
    write_fd(pty.master, b"\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    assert_bytes_contains(&output, &raw_prompt_render("/auth"));
    assert_bytes_contains(
        &output,
        b"Configured: Alibaba Cloud OAuth credentials saved",
    );
    assert!(
        !String::from_utf8_lossy(&output).contains("fake-oauth-access"),
        "raw cloud auth output must not echo OAuth access token"
    );

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "/auth");
    assert_eq!(input.prompt_content, None);

    let cloud_credentials = fs::read_to_string(config_dir.join(".cloud-credentials.yml"))
        .expect("cloud credentials should be written");
    assert!(
        cloud_credentials.contains("mode: OAuth"),
        "{cloud_credentials}"
    );
    assert!(cloud_credentials.contains("region_id: cn-hangzhou"));
    assert!(cloud_credentials.contains("oauth_site_type: CN"));
    assert!(cloud_credentials.contains("oauth_access_token: fake-oauth-access"));
    assert!(cloud_credentials.contains("oauth_refresh_token: fake-oauth-refresh"));
    assert!(cloud_credentials.contains("oauth_access_token_expire: 1780479842"));
    assert!(cloud_credentials.contains("access_key_id: fake-oauth-ak"));
    assert!(cloud_credentials.contains("access_key_secret: fake-oauth-secret"));
    assert!(cloud_credentials.contains("sts_token: fake-oauth-sts"));
    assert!(cloud_credentials.contains("sts_expiration: 1780479844"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}
