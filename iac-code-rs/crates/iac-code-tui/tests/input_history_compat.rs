use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::InputHistory;

#[test]
fn input_history_persists_jsonl_and_loads_legacy_plain_lines() {
    let workspace = TestWorkspace::new("history-jsonl-legacy");
    let history_file = workspace.path().join("history.txt");
    history_file
        .write_text("old one\nold two\n")
        .expect("write legacy history");

    let mut history = InputHistory::new(&history_file);
    assert_eq!(history.search("old"), vec!["old two", "old one"]);

    history.append("line1\nline2").expect("append multiline");
    let raw_lines = history_file
        .read_text()
        .expect("history should be readable")
        .lines()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    assert_eq!(
        raw_lines,
        vec![
            r#"{"format":"iac-code-input-history-v1","text":"old one"}"#,
            r#"{"format":"iac-code-input-history-v1","text":"old two"}"#,
            r#"{"format":"iac-code-input-history-v1","text":"line1\nline2"}"#
        ]
    );

    let reloaded = InputHistory::new(&history_file);
    assert_eq!(reloaded.search("line1"), vec!["line1\nline2"]);
    assert_eq!(
        reloaded.entries(),
        vec!["old one", "old two", "line1\nline2"]
    );
}

#[test]
fn input_history_tracks_session_only_entries_without_persisting_them() {
    let workspace = TestWorkspace::new("history-session-only");
    let history_file = workspace.path().join("history.txt");
    let mut history = InputHistory::new(&history_file);

    history.append("persisted").expect("append persisted");
    history
        .append_session_only("/auth login")
        .expect("append session-only");

    assert_eq!(history.search(""), vec!["/auth login", "persisted"]);
    assert_eq!(
        history.navigate(-1, "draft"),
        Some("/auth login".to_owned())
    );
    assert_eq!(history.navigate(-1, ""), Some("persisted".to_owned()));

    let reloaded = InputHistory::new(&history_file);
    assert_eq!(reloaded.search(""), vec!["persisted"]);
}

#[test]
fn input_history_dedupes_only_consecutive_entries_and_resets_navigation() {
    let workspace = TestWorkspace::new("history-dedup");
    let history_file = workspace.path().join("history.txt");
    let mut history = InputHistory::new(&history_file);

    history.append("first").expect("append first");
    history.append("second").expect("append second");
    assert_eq!(history.navigate(-1, "draft"), Some("second".to_owned()));
    assert!(history.is_navigating());

    history.append("second").expect("append duplicate");
    assert!(!history.is_navigating());
    assert_eq!(history.entries(), vec!["first", "second"]);

    history.append("first").expect("append non-consecutive");
    assert_eq!(history.search("first"), vec!["first", "first"]);
}

#[test]
fn input_history_restores_saved_input_when_navigating_past_newest() {
    let workspace = TestWorkspace::new("history-navigation");
    let history_file = workspace.path().join("history.txt");
    let mut history = InputHistory::new(&history_file);

    history.append("entry").expect("append entry");

    assert_eq!(
        history.navigate(-1, "current input"),
        Some("entry".to_owned())
    );
    assert_eq!(history.saved_input(), "current input");
    assert_eq!(history.navigate(1, ""), None);
    assert!(!history.is_navigating());
    assert_eq!(history.saved_input(), "current input");

    history.reset_navigation();
    assert_eq!(history.saved_input(), "");
}

#[test]
fn input_history_treats_malformed_jsonl_as_literal_legacy_entries() {
    let workspace = TestWorkspace::new("history-malformed");
    let history_file = workspace.path().join("history.txt");
    history_file
        .write_text(
            "{\"format\":\"iac-code-input-history-v1\",\"text\":\"ok\"}\n{\"text\":123}\n[1,2]\n{broken\n",
        )
        .expect("write malformed history");

    let history = InputHistory::new(&history_file);

    assert_eq!(
        history.search(""),
        vec!["{broken", "[1,2]", "{\"text\":123}", "ok"]
    );
}

#[test]
fn input_history_reports_persistence_errors() {
    let workspace = TestWorkspace::new("history-write-error");
    let history_file = workspace.path().join("history.txt");
    fs::create_dir(&history_file).expect("create path that cannot be overwritten by file");
    let mut history = InputHistory::new(&history_file);

    let error = history
        .append("cannot persist")
        .expect_err("append should report persistence errors");

    assert!(
        !error.to_string().is_empty(),
        "error should describe the persistence failure"
    );
    assert_eq!(history.search("cannot"), vec!["cannot persist"]);
}

#[cfg(unix)]
#[test]
fn input_history_file_is_owner_only() {
    use std::os::unix::fs::PermissionsExt;

    let workspace = TestWorkspace::new("history-permissions");
    let history_file = workspace.path().join("history.txt");
    let mut history = InputHistory::new(&history_file);

    history
        .append("secret-ish prompt")
        .expect("append history entry");

    assert_eq!(
        fs::metadata(&history_file)
            .expect("history metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

struct TestWorkspace {
    path: PathBuf,
}

impl TestWorkspace {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("iac-code-rs-tui-{name}-{unique}"));
        fs::create_dir_all(&path).expect("create test workspace");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.path).ok();
    }
}

trait PathTextExt {
    fn read_text(&self) -> std::io::Result<String>;
    fn write_text(&self, text: &str) -> std::io::Result<()>;
}

impl PathTextExt for Path {
    fn read_text(&self) -> std::io::Result<String> {
        fs::read_to_string(self)
    }

    fn write_text(&self, text: &str) -> std::io::Result<()> {
        fs::write(self, text)
    }
}
