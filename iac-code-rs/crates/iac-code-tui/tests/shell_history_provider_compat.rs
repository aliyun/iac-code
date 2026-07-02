use std::fs;
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::{read_shell_history, CompletionToken, ShellHistoryProvider, SuggestionProvider};

static TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

#[test]
fn shell_history_provider_matches_dedupes_and_orders_like_python() {
    let history_file = temp_dir("shell-history-provider").join(".bash_history");
    fs::write(
        &history_file,
        "git status\n\
git commit -m 'first'\n\
ls -la\n\
git push origin main\n\
python -m pytest\n\
git status\n\
docker build .\n",
    )
    .expect("history should be written");

    let provider = ShellHistoryProvider::with_history_path(Some(history_file), 100);
    let items = provider.provide(&token("!git"));

    assert_eq!(items[0].display_text, "git status");
    assert!(items
        .iter()
        .all(|item| item.display_text.to_ascii_lowercase().contains("git")));
    let displays = items
        .iter()
        .map(|item| item.display_text.as_str())
        .collect::<Vec<_>>();
    assert_eq!(displays.len(), 3);
    assert_eq!(
        displays,
        vec![
            "git status",
            "git push origin main",
            "git commit -m 'first'"
        ]
    );
    assert!(items.iter().all(|item| item.source == "shell"));
    assert!(items.iter().all(|item| item.icon.as_deref() == Some("↑")));
    assert!(items.iter().all(|item| item.completion.starts_with('!')));
}

#[test]
fn shell_history_provider_empty_query_returns_recent_unique_entries() {
    let history_file = temp_dir("shell-history-empty-query").join(".bash_history");
    fs::write(
        &history_file,
        "git status\nls -la\ngit status\ndocker build .\n",
    )
    .expect("history should be written");

    let provider = ShellHistoryProvider::with_history_path(Some(history_file), 100);
    let items = provider.provide(&token("!"));

    assert_eq!(
        items
            .iter()
            .map(|item| item.display_text.as_str())
            .collect::<Vec<_>>(),
        vec!["docker build .", "git status", "ls -la"]
    );
}

#[test]
fn shell_history_provider_accepts_token_without_bang_prefix_and_limits_results() {
    let history_file = temp_dir("shell-history-limit").join(".bash_history");
    fs::write(
        &history_file,
        (0..10)
            .map(|index| format!("git command {index}"))
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .expect("history should be written");

    let provider = ShellHistoryProvider::with_history_path(Some(history_file), 3);
    let items = provider.provide(&CompletionToken {
        text: "git".to_owned(),
        start: 0,
        end: 3,
        trigger: "!".to_owned(),
    });

    assert_eq!(
        items
            .iter()
            .map(|item| item.display_text.as_str())
            .collect::<Vec<_>>(),
        vec!["git command 9", "git command 8", "git command 7"]
    );
}

#[test]
fn shell_history_provider_returns_empty_without_history_path() {
    let provider = ShellHistoryProvider::with_history_path(None, 100);

    assert!(provider.provide(&token("!git")).is_empty());
}

#[test]
fn read_shell_history_parses_zsh_extended_history_like_python() {
    let history_file = temp_dir("shell-history-zsh").join(".zsh_history");
    fs::write(
        &history_file,
        ": 1700000000:0;\nplain\n\n: 1700000001:0;git status\n: 1700000002:0;git diff\n",
    )
    .expect("history should be written");

    assert_eq!(
        read_shell_history(&history_file),
        vec!["plain", "git status", "git diff"]
    );
}

#[test]
fn read_shell_history_returns_empty_for_missing_file() {
    assert!(read_shell_history(temp_dir("shell-history-missing").join("missing")).is_empty());
}

fn token(text: &str) -> CompletionToken {
    CompletionToken {
        text: text.to_owned(),
        start: 0,
        end: text.len(),
        trigger: "!".to_owned(),
    }
}

fn temp_dir(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock should be after epoch")
        .as_nanos();
    let index = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("{name}-{}-{nonce}-{index}", process::id()));
    fs::create_dir_all(&dir).expect("temp dir should be created");
    dir
}
