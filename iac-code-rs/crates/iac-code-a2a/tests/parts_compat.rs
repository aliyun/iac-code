use std::ffi::OsString;
use std::sync::{Mutex, MutexGuard, OnceLock};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_a2a::parts::{part_to_prompt, parts_to_prompt, supported_input_mime_types, A2APart};
use iac_code_protocol::json;

#[test]
fn text_data_raw_and_joined_parts_convert_to_prompt() {
    let _guard = clear_a2a_env();
    let root = temp_root("basic");

    assert_eq!(
        part_to_prompt(&A2APart::text("create a vpc"), &root).unwrap(),
        "create a vpc"
    );
    assert_eq!(
        part_to_prompt(
            &A2APart::data(json::object([
                ("count", json::float(2.0)),
                ("template", json::string("value")),
            ])),
            &root,
        )
        .unwrap(),
        r#"{"count":2.0,"template":"value"}"#
    );
    assert_eq!(
        part_to_prompt(&A2APart::raw(b"name: vpc\n".to_vec(), "text/yaml"), &root).unwrap(),
        "name: vpc\n"
    );
    assert_eq!(
        parts_to_prompt(
            &[
                A2APart::text("first"),
                A2APart::text(""),
                A2APart::text("second"),
            ],
            &root,
        )
        .unwrap(),
        "first\nsecond"
    );
}

#[test]
fn text_part_accepts_extra_text_mime_type_from_env() {
    let _guard = env_guard();
    let root = temp_root("extra-mime");
    std::env::set_var("IACCODE_A2A_TEXT_MIME_TYPES", "application/vnd.iac+yaml");

    assert!(supported_input_mime_types().contains(&"application/vnd.iac+yaml".to_owned()));
    assert_eq!(
        part_to_prompt(
            &A2APart::text_with_media_type("Resources: {}", "application/vnd.iac+yaml"),
            &root,
        )
        .unwrap(),
        "Resources: {}"
    );

    std::env::remove_var("IACCODE_A2A_TEXT_MIME_TYPES");
}

#[test]
fn supported_input_mime_types_sort_extra_values_like_python() {
    let _guard = env_guard();
    let previous_text = std::env::var_os("IACCODE_A2A_TEXT_MIME_TYPES");
    let previous_multimodal = std::env::var_os("IACCODE_A2A_MULTIMODAL_MIME_TYPES");
    std::env::set_var(
        "IACCODE_A2A_TEXT_MIME_TYPES",
        "application/zeta, application/alpha;application/zeta",
    );
    std::env::remove_var("IACCODE_A2A_MULTIMODAL_MIME_TYPES");

    let values = supported_input_mime_types();
    restore_env_var("IACCODE_A2A_TEXT_MIME_TYPES", previous_text);
    restore_env_var("IACCODE_A2A_MULTIMODAL_MIME_TYPES", previous_multimodal);

    let extras = values
        .iter()
        .filter(|value| {
            value.as_str() == "application/alpha" || value.as_str() == "application/zeta"
        })
        .cloned()
        .collect::<Vec<_>>();
    assert_eq!(extras, vec!["application/alpha", "application/zeta"]);
}

#[test]
fn file_url_text_part_reads_file_inside_workspace() {
    let _guard = clear_a2a_env();
    let root = temp_root("file-url");
    std::fs::create_dir_all(&root).unwrap();
    let source = root.join("template.yaml");
    std::fs::write(&source, "ROSTemplateFormatVersion: '2015-09-01'\n").unwrap();

    assert_eq!(
        part_to_prompt(&A2APart::url(file_url(&source), "text/plain"), &root).unwrap(),
        "ROSTemplateFormatVersion: '2015-09-01'\n"
    );
}

#[test]
fn file_url_honors_allowed_cwd_roots_like_python() {
    let _guard = env_guard();
    let root = temp_root("allowed-cwd-root");
    let allowed_root = temp_root("allowed-cwd-other");
    std::fs::create_dir_all(&root).unwrap();
    std::fs::create_dir_all(&allowed_root).unwrap();
    let source = root.join("template.yaml");
    std::fs::write(&source, "ROSTemplateFormatVersion: '2015-09-01'\n").unwrap();

    let previous = std::env::var_os("IACCODE_A2A_ALLOWED_CWDS");
    std::env::set_var("IACCODE_A2A_ALLOWED_CWDS", &allowed_root);
    let result = part_to_prompt(&A2APart::url(file_url(&source), "text/plain"), &root);
    if let Some(previous) = previous {
        std::env::set_var("IACCODE_A2A_ALLOWED_CWDS", previous);
    } else {
        std::env::remove_var("IACCODE_A2A_ALLOWED_CWDS");
    }

    let error = result.unwrap_err().to_string();
    assert!(error.contains("outside the allowed workspace"));
    assert!(!error.contains(source.to_string_lossy().as_ref()));
}

#[test]
fn multimodal_raw_file_and_binary_data_parts_emit_manifest() {
    let _guard = clear_a2a_env();
    let root = temp_root("manifest");
    std::fs::create_dir_all(&root).unwrap();
    let source = root.join("voice.wav");
    std::fs::write(&source, b"RIFFaudio").unwrap();

    let raw_prompt = part_to_prompt(
        &A2APart::raw_with_filename(
            b"\x89PNG\r\n\x1a\nimage-bytes".to_vec(),
            "image/png",
            "diagram.png",
        ),
        &root,
    )
    .unwrap();
    assert!(raw_prompt.contains("A2A multimodal attachment:"));
    assert!(raw_prompt.contains("filename=diagram.png"));
    assert!(raw_prompt.contains("mediaType=image/png"));
    assert!(raw_prompt.contains("byteSize=19"));
    assert!(raw_prompt.contains("sha256="));
    assert!(!raw_prompt.contains("image-bytes"));

    let file_prompt = part_to_prompt(&A2APart::url(file_url(&source), "audio/wav"), &root).unwrap();
    assert!(file_prompt.contains("filename=voice.wav"));
    assert!(file_prompt.contains("byteSize=9"));
    assert!(file_prompt.contains(&format!(
        "source={}",
        file_url(&source.canonicalize().unwrap())
    )));

    let encoded = STANDARD.encode(b"\x00\x01binary");
    let data_prompt = part_to_prompt(
        &A2APart::data_with_media_type(
            json::object([
                ("bytes", json::string(encoded)),
                ("filename", json::string("sample.bin")),
            ]),
            "application/octet-stream",
        ),
        &root,
    )
    .unwrap();
    assert!(data_prompt.contains("filename=sample.bin"));
    assert!(data_prompt.contains("mediaType=application/octet-stream"));
    assert!(data_prompt.contains("byteSize=8"));
}

#[test]
fn part_rejects_unsupported_or_unsafe_inputs() {
    let _guard = clear_a2a_env();
    let root = temp_root("reject");
    std::fs::create_dir_all(&root).unwrap();
    let outside = root.parent().unwrap().join("outside.txt");
    std::fs::write(&outside, "secret").unwrap();
    let directory = root.join("directory");
    std::fs::create_dir_all(&directory).unwrap();

    assert!(part_to_prompt(
        &A2APart::text_with_media_type("bad", "application/octet-stream"),
        &root
    )
    .unwrap_err()
    .to_string()
    .contains("unsupported media type"));
    assert!(
        part_to_prompt(&A2APart::raw(vec![0xff], "text/plain"), &root)
            .unwrap_err()
            .to_string()
            .contains("UTF-8")
    );
    assert!(part_to_prompt(
        &A2APart::url("https://example.com/template.yaml", "text/plain"),
        &root
    )
    .unwrap_err()
    .to_string()
    .contains("local file://"));
    let outside_error = part_to_prompt(&A2APart::url(file_url(&outside), "text/plain"), &root)
        .unwrap_err()
        .to_string();
    assert!(outside_error.contains("outside the allowed workspace"));
    assert!(!outside_error.contains(outside.to_string_lossy().as_ref()));
    assert!(
        part_to_prompt(&A2APart::url(file_url(&directory), "text/plain"), &root)
            .unwrap_err()
            .to_string()
            .contains("existing file")
    );
}

fn file_url(path: &std::path::Path) -> String {
    format!("file://{}", path.to_string_lossy())
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root =
        std::env::temp_dir().join(format!("iac-code-a2a-parts-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    root
}

struct EnvGuard {
    _guard: MutexGuard<'static, ()>,
    previous: Vec<(&'static str, Option<OsString>)>,
}

fn env_guard() -> EnvGuard {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    let guard = LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let previous = A2A_ENV_VARS
        .iter()
        .map(|name| (*name, std::env::var_os(name)))
        .collect();
    EnvGuard {
        _guard: guard,
        previous,
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in self.previous.drain(..) {
            restore_env_var(name, value);
        }
    }
}

fn clear_a2a_env() -> EnvGuard {
    let guard = env_guard();
    for name in A2A_ENV_VARS {
        std::env::remove_var(name);
    }
    guard
}

const A2A_ENV_VARS: &[&str] = &[
    "IACCODE_A2A_ALLOWED_CWDS",
    "IACCODE_A2A_MULTIMODAL_MIME_TYPES",
    "IACCODE_A2A_TEXT_MIME_TYPES",
];

fn restore_env_var(name: &str, value: Option<OsString>) {
    if let Some(value) = value {
        std::env::set_var(name, value);
    } else {
        std::env::remove_var(name);
    }
}
