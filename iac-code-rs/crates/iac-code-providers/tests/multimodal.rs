use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_providers::{
    builtin_multimodal_models, get_multimodal_spec, is_model_multimodal,
    is_model_multimodal_with_probe, probe_openapi_compatible, AutoDetectCache,
    MultimodalProbeOptions,
};

#[test]
fn builtin_claude_opus_supports_images() {
    let spec = get_multimodal_spec("claude-opus-4-7", None);
    assert!(spec.support_multimodal);
    assert!(spec.formats.iter().any(|format| format == "image/png"));
    assert_eq!(spec.max_images_per_message, 20);
}

#[test]
fn unknown_model_defaults_to_no_images() {
    let spec = get_multimodal_spec("does-not-exist-1.0", None);
    assert!(!spec.support_multimodal);
    assert!(!is_model_multimodal("does-not-exist-1.0", None));
}

#[test]
fn settings_override_wins_over_builtin_and_defaults_missing_fields() {
    let settings =
        write_settings("multiModal:\n  models:\n    custom-vl: {supportMultimodal: true}\n");

    let spec = get_multimodal_spec("custom-vl", Some(&settings.path));
    assert!(spec.support_multimodal);
    assert!(spec.formats.iter().any(|format| format == "image/webp"));
    assert_eq!(spec.max_images_per_message, 20);
}

#[test]
fn settings_override_can_disable_builtin() {
    let settings =
        write_settings("multiModal:\n  models:\n    claude-opus-4-7: {supportMultimodal: false}\n");

    assert!(!is_model_multimodal(
        "claude-opus-4-7",
        Some(&settings.path)
    ));
}

#[test]
fn settings_override_can_customize_formats_and_limit() {
    let settings = write_settings(
        "multiModal:\n  models:\n    custom-vl:\n      supportMultimodal: true\n      formats:\n      - image/png\n      - image/bmp\n      maxImagesPerMessage: 3\n",
    );

    let spec = get_multimodal_spec("custom-vl", Some(&settings.path));
    assert!(spec.support_multimodal);
    assert_eq!(
        spec.formats,
        vec!["image/png".to_owned(), "image/bmp".to_owned()]
    );
    assert_eq!(spec.max_images_per_message, 3);
}

#[test]
fn builtin_set_includes_registry_flagged_models() {
    let builtin = builtin_multimodal_models();
    assert!(builtin.contains("claude-opus-4-7"));
    assert!(builtin.contains("gpt-5.5"));
    assert!(builtin.contains("gemini-2.5-pro"));
    assert!(builtin.contains("qwen3.6-plus"));
    assert!(builtin.contains("kimi-k2.6"));
}

#[test]
fn builtin_set_excludes_non_multimodal_models() {
    let builtin = builtin_multimodal_models();
    assert!(!builtin.contains("deepseek-v4-pro"));
    assert!(!builtin.contains("qwen3-coder-plus"));
}

#[test]
fn probe_returns_true_when_modalities_include_image() {
    let (base_url, server) = spawn_models_server(
        Some("Bearer x"),
        r#"{"data":[{"id":"custom-vl","architecture":{"input_modalities":["text","image"]}}]}"#,
    );

    let result =
        probe_openapi_compatible(&base_url, Some("x"), "custom-vl", Duration::from_secs(2));
    server.join().expect("server thread should finish");

    assert_eq!(result, Some(true));
}

#[test]
fn probe_returns_none_on_unknown_schema() {
    let (base_url, server) = spawn_models_server(None, r#"{"data":[{"id":"custom-vl"}]}"#);

    let result = probe_openapi_compatible(&base_url, None, "custom-vl", Duration::from_secs(2));
    server.join().expect("server thread should finish");

    assert_eq!(result, None);
}

#[test]
fn cache_round_trip() {
    let settings = temp_settings_dir();
    let mut cache = AutoDetectCache::new(settings.cache_path.clone());
    cache.set("https://x/v1", "custom-vl", true);
    cache.flush().expect("cache should flush");

    let fresh = AutoDetectCache::new(settings.cache_path.clone());
    assert_eq!(fresh.get("https://x/v1", "custom-vl"), Some(true));
    assert_eq!(fresh.get("https://x/v1", "other"), None);
}

#[test]
fn cache_flush_leaves_no_partial_temp_files() {
    let settings = temp_settings_dir();
    let mut cache = AutoDetectCache::new(settings.cache_path.clone());
    cache.set("https://x/v1", "m", true);
    cache.flush().expect("cache should flush");

    let leftovers = fs::read_dir(&settings.dir)
        .expect("temp config dir should be readable")
        .map(|entry| entry.expect("dir entry should be readable").file_name())
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| name.starts_with(".multimodal-cache."))
        .collect::<Vec<_>>();
    assert_eq!(leftovers, vec![".multimodal-cache.yml".to_owned()]);
}

#[test]
fn cache_flush_last_writer_wins_with_valid_yaml() {
    let settings = temp_settings_dir();
    let mut first = AutoDetectCache::new(settings.cache_path.clone());
    first.set("https://x/v1", "m1", true);
    first.flush().expect("first cache should flush");

    let mut second = AutoDetectCache::new(settings.cache_path.clone());
    second.set("https://x/v1", "m2", false);
    second.flush().expect("second cache should flush");

    let fresh = AutoDetectCache::new(settings.cache_path.clone());
    assert_eq!(fresh.get("https://x/v1", "m1"), Some(true));
    assert_eq!(fresh.get("https://x/v1", "m2"), Some(false));
}

#[test]
fn openapi_compatible_detection_probes_and_persists_cache() {
    let settings = temp_settings_dir();
    let (base_url, server) = spawn_models_server(
        Some("Bearer x"),
        r#"{"data":[{"id":"custom-vl","architecture":{"input_modalities":["text","image"]}}]}"#,
    );

    let result = is_model_multimodal_with_probe(
        "custom-vl",
        MultimodalProbeOptions {
            settings_path: Some(&settings.path),
            cache_path: Some(&settings.cache_path),
            provider_key: Some("openapi_compatible"),
            base_url: Some(&base_url),
            api_key: Some("x"),
            timeout: Duration::from_secs(2),
        },
    );
    server.join().expect("server thread should finish");

    assert!(result);
    let fresh = AutoDetectCache::new(settings.cache_path.clone());
    assert_eq!(fresh.get(&base_url, "custom-vl"), Some(true));
}

struct TempSettings {
    dir: std::path::PathBuf,
    path: std::path::PathBuf,
    cache_path: std::path::PathBuf,
}

static TEMP_SETTINGS_COUNTER: AtomicU64 = AtomicU64::new(0);

impl Drop for TempSettings {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn write_settings(content: &str) -> TempSettings {
    let settings = temp_settings_dir();
    fs::write(&settings.path, content).expect("settings should be written");
    settings
}

fn temp_settings_dir() -> TempSettings {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock should be after epoch")
        .as_nanos();
    let counter = TEMP_SETTINGS_COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "iac-code-rs-multimodal-{}-{counter}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&dir).expect("temp config dir should be created");
    let path = dir.join("settings.yml");
    let cache_path = dir.join(".multimodal-cache.yml");
    TempSettings {
        dir,
        path,
        cache_path,
    }
}

fn spawn_models_server(
    expected_auth: Option<&'static str>,
    response_body: &'static str,
) -> (String, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("GET /v1/models HTTP/1.1"));
        if let Some(expected_auth) = expected_auth {
            assert!(request.contains(&format!("authorization: {expected_auth}")));
        }
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            response_body.len(),
            response_body
        )
        .expect("write response");
    });
    (format!("http://{addr}/v1"), server)
}

fn read_http_request(stream: &mut std::net::TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set read timeout");
    let mut buffer = [0_u8; 4096];
    let mut request = Vec::new();
    loop {
        let bytes = stream.read(&mut buffer).expect("read request");
        if bytes == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..bytes]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    String::from_utf8_lossy(&request).into_owned()
}
