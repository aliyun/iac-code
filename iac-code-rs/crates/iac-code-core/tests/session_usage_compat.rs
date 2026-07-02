use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_core::{sanitize_path, SessionUsageStore, SessionUsageTotals, USAGE_JSONL_FILENAME};
use iac_code_protocol::Usage;

const CWD: &str = "/tmp/status-project";

#[test]
fn session_usage_totals_adds_usage_and_tracks_record_count() {
    let mut totals = SessionUsageTotals::default();

    assert!(totals.add(&Usage {
        input_tokens: 10,
        output_tokens: 5,
        cache_read_input_tokens: 3,
        cache_creation_input_tokens: 2,
    }));
    assert!(totals.add(&Usage {
        input_tokens: 7,
        output_tokens: 1,
        ..Usage::default()
    }));

    assert_eq!(totals.input_tokens, 17);
    assert_eq!(totals.output_tokens, 6);
    assert_eq!(totals.cache_read_input_tokens, 3);
    assert_eq!(totals.cache_creation_input_tokens, 2);
    assert_eq!(totals.total_tokens(), 23);
    assert_eq!(totals.recorded_events, 2);
    assert!(totals.has_recorded_usage());
}

#[test]
fn session_usage_store_skips_zero_usage_without_creating_file() {
    let root = unique_temp_dir("iac-code-rs-session-usage-zero");
    let store = SessionUsageStore::new(&root);

    assert!(!store
        .append(
            CWD,
            "s1",
            &Usage::default(),
            Some("dashscope"),
            Some("qwen3.7-max")
        )
        .expect("zero usage append should not error"));

    assert!(!store.path_for(CWD, "s1").exists());
    assert!(!store.load(CWD, "s1").has_recorded_usage());

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_usage_store_appends_and_loads_new_directory_sidecar() {
    let root = unique_temp_dir("iac-code-rs-session-usage-roundtrip");
    let store = SessionUsageStore::new(&root);

    assert!(store
        .append(
            CWD,
            "s2",
            &Usage {
                input_tokens: 12,
                output_tokens: 3,
                ..Usage::default()
            },
            Some("dashscope"),
            Some("qwen3.7-max"),
        )
        .expect("usage should append"));
    assert!(store
        .append(
            CWD,
            "s2",
            &Usage {
                input_tokens: 5,
                output_tokens: 2,
                cache_read_input_tokens: 4,
                cache_creation_input_tokens: 1,
            },
            Some("dashscope"),
            Some("qwen3.7-max"),
        )
        .expect("usage should append"));

    let totals = store.load(CWD, "s2");
    assert_eq!(totals.input_tokens, 17);
    assert_eq!(totals.output_tokens, 5);
    assert_eq!(totals.cache_read_input_tokens, 4);
    assert_eq!(totals.cache_creation_input_tokens, 1);
    assert_eq!(totals.total_tokens(), 22);
    assert_eq!(totals.recorded_events, 2);

    let raw = fs::read_to_string(store.path_for(CWD, "s2")).expect("usage sidecar should exist");
    assert!(raw.contains(r#""type":"usage""#), "{raw}");
    assert!(raw.contains(r#""version":1"#), "{raw}");
    assert!(raw.contains(r#""provider":"dashscope""#), "{raw}");
    assert!(raw.contains(r#""model":"qwen3.7-max""#), "{raw}");
    assert!(raw.contains(r#""created_at":""#), "{raw}");

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_usage_store_skips_corrupt_and_unrelated_rows() {
    let root = unique_temp_dir("iac-code-rs-session-usage-corrupt");
    let store = SessionUsageStore::new(&root);
    let path = store.path_for(CWD, "s3");
    fs::create_dir_all(path.parent().expect("usage path parent")).expect("create usage dir");
    fs::write(
        &path,
        concat!(
            "{\"type\":\"usage\",\"version\":1,\"input_tokens\":4,\"output_tokens\":6,",
            "\"cache_read_input_tokens\":1,\"cache_creation_input_tokens\":0}\n",
            "not json\n",
            "{\"type\":\"last-prompt\",\"last_prompt\":\"ignored\"}\n",
            "{\"type\":\"usage\",\"version\":1,\"input_tokens\":3,\"output_tokens\":2}\n",
        ),
    )
    .expect("write mixed usage rows");

    let totals = store.load(CWD, "s3");

    assert_eq!(totals.input_tokens, 7);
    assert_eq!(totals.output_tokens, 8);
    assert_eq!(totals.cache_read_input_tokens, 1);
    assert_eq!(totals.cache_creation_input_tokens, 0);
    assert_eq!(totals.total_tokens(), 15);
    assert_eq!(totals.recorded_events, 2);

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_usage_store_loads_legacy_numeric_values_like_python() {
    let root = unique_temp_dir("iac-code-rs-session-usage-numeric-values");
    let store = SessionUsageStore::new(&root);
    let path = store.path_for(CWD, "s3-numeric");
    fs::create_dir_all(path.parent().expect("usage path parent")).expect("create usage dir");
    fs::write(
        &path,
        concat!(
            "{\"type\":\"usage\",\"version\":1,",
            "\"input_tokens\":\"7\",\"output_tokens\":2.9,",
            "\"cache_read_input_tokens\":true,\"cache_creation_input_tokens\":-5}\n",
            "{\"type\":\"usage\",\"version\":1,",
            "\"input_tokens\":\"not-a-number\",\"output_tokens\":\"11\"}\n",
        ),
    )
    .expect("write mixed numeric usage rows");

    let totals = store.load(CWD, "s3-numeric");

    assert_eq!(totals.input_tokens, 7);
    assert_eq!(totals.output_tokens, 13);
    assert_eq!(totals.cache_read_input_tokens, 0);
    assert_eq!(totals.cache_creation_input_tokens, 0);
    assert_eq!(totals.recorded_events, 2);

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_usage_store_paths_match_python_directory_and_legacy_layouts() {
    let root = unique_temp_dir("iac-code-rs-session-usage-paths");
    let store = SessionUsageStore::new(&root);

    assert_eq!(
        store.path_for(CWD, "s4"),
        root.join(sanitize_path(CWD))
            .join("s4")
            .join(USAGE_JSONL_FILENAME)
    );
    assert_eq!(
        store.legacy_path_for(CWD, "s4"),
        root.join(sanitize_path(CWD)).join("s4.usage.jsonl")
    );

    fs::remove_dir_all(root).ok();
}

#[test]
fn session_usage_store_loads_new_and_legacy_sidecars() {
    let root = unique_temp_dir("iac-code-rs-session-usage-legacy");
    let store = SessionUsageStore::new(&root);
    let new_path = store.path_for(CWD, "s5");
    let legacy_path = store.legacy_path_for(CWD, "s5");
    fs::create_dir_all(new_path.parent().expect("new usage parent")).expect("create new dir");
    fs::create_dir_all(legacy_path.parent().expect("legacy usage parent"))
        .expect("create legacy dir");
    fs::write(
        &new_path,
        concat!(
            "{\"type\":\"usage\",\"version\":1,\"input_tokens\":4,\"output_tokens\":6,",
            "\"cache_read_input_tokens\":1,\"cache_creation_input_tokens\":0}\n",
        ),
    )
    .expect("write new sidecar");
    fs::write(
        &legacy_path,
        concat!(
            "{\"type\":\"usage\",\"version\":1,\"input_tokens\":3,\"output_tokens\":2,",
            "\"cache_read_input_tokens\":5,\"cache_creation_input_tokens\":7}\n",
        ),
    )
    .expect("write legacy sidecar");

    let totals = store.load(CWD, "s5");

    assert_eq!(totals.input_tokens, 7);
    assert_eq!(totals.output_tokens, 8);
    assert_eq!(totals.cache_read_input_tokens, 6);
    assert_eq!(totals.cache_creation_input_tokens, 7);
    assert_eq!(totals.total_tokens(), 15);
    assert_eq!(totals.recorded_events, 2);

    fs::remove_dir_all(root).ok();
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}
