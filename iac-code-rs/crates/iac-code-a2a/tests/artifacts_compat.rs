use iac_code_a2a::artifacts::{A2AArtifactStore, UnsafeArtifactNameError};

#[test]
fn artifact_store_writes_text_and_metadata() {
    let root = temp_root("text");
    let store = A2AArtifactStore::new(&root);

    let metadata = store
        .save_text(
            "template.yaml",
            "ROSTemplateFormatVersion: '2015-09-01'",
            "text/yaml",
        )
        .expect("save text");

    assert_eq!(metadata.filename, "template.yaml");
    assert!(metadata.byte_size > 0);
    assert_eq!(
        metadata.sha256,
        "aaa464ea34dcdb09b7f5f134d49ab24fd1c8e2241db9dd300d6292d4b52bf612"
    );
    assert!(metadata.uri.starts_with("file://"));
    let path = store.path_for(&metadata.artifact_id).expect("path");
    assert!(std::fs::read_to_string(path)
        .expect("read")
        .starts_with("ROSTemplate"));
}

#[test]
fn artifact_store_writes_binary_and_metadata() {
    let root = temp_root("binary");
    let store = A2AArtifactStore::new(&root);

    let metadata = store
        .save_bytes("diagram.png", b"\x89PNG\r\n\x1a\nimage", "image/png")
        .expect("save bytes");

    assert_eq!(metadata.filename, "diagram.png");
    assert_eq!(metadata.media_type, "image/png");
    assert_eq!(metadata.byte_size, 13);
    assert!(metadata.uri.starts_with("file://"));
    assert_eq!(
        std::fs::read(store.path_for(&metadata.artifact_id).expect("path")).expect("read"),
        b"\x89PNG\r\n\x1a\nimage"
    );
}

#[test]
fn artifact_ids_match_python_uuid4_shape() {
    let root = temp_root("uuid");
    let store = A2AArtifactStore::new(&root);

    let first = store
        .save_text("first.txt", "first", "text/plain")
        .expect("save first");
    let second = store
        .save_text("second.txt", "second", "text/plain")
        .expect("save second");

    assert_uuid4_shape(&first.artifact_id);
    assert_uuid4_shape(&second.artifact_id);
    assert_ne!(first.artifact_id, second.artifact_id);
}

#[test]
fn artifact_file_uri_percent_encodes_paths_like_python_as_uri() {
    let root = temp_root("uri encoding");
    let store = A2AArtifactStore::new(&root);

    let metadata = store
        .save_text("template output.yaml", "content", "text/yaml")
        .expect("save text");

    assert!(metadata.uri.starts_with("file://"));
    assert!(metadata.uri.contains("uri%20encoding"), "{}", metadata.uri);
    assert!(
        metadata.uri.ends_with("template%20output.yaml"),
        "{}",
        metadata.uri
    );
    assert!(!metadata.uri.contains(' '), "{}", metadata.uri);
}

#[cfg(unix)]
#[test]
fn artifact_filename_allows_backslash_on_posix_like_python_basename() {
    let root = temp_root("backslash");
    let store = A2AArtifactStore::new(&root);

    let metadata = store
        .save_text("folder\\template.yaml", "content", "text/yaml")
        .expect("POSIX basename should allow backslash");

    assert_eq!(metadata.filename, "folder\\template.yaml");
    assert!(
        metadata.uri.ends_with("folder%5Ctemplate.yaml"),
        "{}",
        metadata.uri
    );
    assert!(
        std::fs::read_to_string(store.path_for(&metadata.artifact_id).expect("path"))
            .expect("read")
            .contains("content")
    );
}

#[test]
fn artifact_store_decodes_base64_content() {
    let root = temp_root("base64");
    let store = A2AArtifactStore::new(&root);

    let metadata = store
        .save_base64("sample.bin", "AAFiYXNlNjQ=", "application/octet-stream")
        .expect("save base64");

    assert_eq!(metadata.byte_size, 8);
    assert_eq!(
        std::fs::read(store.path_for(&metadata.artifact_id).expect("path")).expect("read"),
        b"\x00\x01base64"
    );
}

#[test]
fn artifact_store_rejects_path_traversal() {
    let root = temp_root("unsafe");
    let store = A2AArtifactStore::new(&root);

    assert_eq!(
        store
            .save_text("../secret.txt", "bad", "text/plain")
            .expect_err("unsafe filename"),
        UnsafeArtifactNameError
    );
    assert_eq!(
        store
            .save_text(".", "bad", "text/plain")
            .expect_err("unsafe filename"),
        UnsafeArtifactNameError
    );
}

fn assert_uuid4_shape(value: &str) {
    let bytes = value.as_bytes();
    assert_eq!(bytes.len(), 36, "{value}");
    for index in [8, 13, 18, 23] {
        assert_eq!(bytes[index], b'-', "{value}");
    }
    assert_eq!(bytes[14], b'4', "{value}");
    assert!(matches!(bytes[19], b'8' | b'9' | b'a' | b'b'), "{value}");
    assert!(
        bytes.iter().enumerate().all(|(index, byte)| {
            matches!(index, 8 | 13 | 18 | 23)
                || byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()
        }),
        "{value}"
    );
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!(
        "iac-code-a2a-artifacts-{name}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}
