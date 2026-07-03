use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::{
    fuzzy_match, CompletionToken, DirectorySuggestionProvider, FileSuggestionProvider,
    SuggestionProvider,
};

#[test]
fn fuzzy_match_scores_like_python_picker() {
    let exact = fuzzy_match("alpha", "alpha").expect("exact match");
    let prefix = fuzzy_match("alp", "alpha").expect("prefix match");
    let subsequence = fuzzy_match("aph", "alpha").expect("subsequence match");
    let boundary = fuzzy_match("ba", "foo bar").expect("word boundary match");
    let mid = fuzzy_match("oo", "foobar").expect("mid-string match");
    let consecutive = fuzzy_match("abc", "abcdef").expect("consecutive match");
    let sparse = fuzzy_match("abc", "axbxcx").expect("sparse match");

    assert!(exact > prefix);
    assert!(prefix > subsequence);
    assert!(boundary > mid);
    assert!(consecutive > sparse);
    assert_eq!(fuzzy_match("", "anything"), Some(0.0));
    assert_eq!(fuzzy_match("ALPHA", "alpha"), fuzzy_match("alpha", "alpha"));
    assert_eq!(fuzzy_match("xyz", "alpha"), None);
}

#[test]
fn file_provider_indexes_project_files_and_excludes_python_ignored_dirs() {
    let workspace = TestWorkspace::new("file-provider");
    create_sample_tree(workspace.path());
    let provider = FileSuggestionProvider::new(workspace.path());

    assert_eq!(provider.trigger(), "@");

    let all_items = provider.provide(&token("@"));
    let all_paths = display_texts(&all_items);
    assert!(all_paths.contains(&"main.py"));
    assert!(all_paths.contains(&"config.yaml"));
    assert!(all_paths.contains(&"src/app.py"));
    assert!(all_paths.contains(&"src/utils.py"));
    assert!(all_paths.contains(&"src/ui/input.py"));
    assert!(!all_paths.iter().any(|path| path.contains(".git")));
    assert!(!all_paths.iter().any(|path| path.contains("__pycache__")));
    assert!(!all_paths.iter().any(|path| path.contains("egg-info")));
    assert!(all_items.iter().all(|item| {
        item.source == "file"
            && item.icon.as_deref() == Some("+")
            && item.id.starts_with("file:")
            && item.completion.starts_with('@')
            && item.description.as_deref() == Some("")
    }));

    let app_items = provider.provide(&token("@app"));
    assert!(display_texts(&app_items).contains(&"src/app.py"));
    assert_eq!(provider.provide(&token("@xyzxyzxyz123")), Vec::new());
}

#[test]
fn file_provider_reuses_fresh_index_like_python_provider() {
    let workspace = TestWorkspace::new("file-provider-cache");
    create_sample_tree(workspace.path());
    let provider = FileSuggestionProvider::new(workspace.path());

    let initial_items = provider.provide(&token("@"));
    assert!(display_texts(&initial_items).contains(&"main.py"));

    fs::write(workspace.path().join("newly-added.py"), "# new").expect("write new file");

    let cached_items = provider.provide(&token("@"));
    assert!(!display_texts(&cached_items).contains(&"newly-added.py"));
}

#[test]
fn directory_provider_lists_current_directory_like_python_provider() {
    let workspace = TestWorkspace::new("directory-provider");
    create_sample_tree(workspace.path());
    let provider = DirectorySuggestionProvider::new(workspace.path());

    assert_eq!(provider.trigger(), "@");

    let root_items = provider.provide(&token("@"));
    let root_names = display_texts(&root_items);
    assert!(root_names.contains(&"src/"));
    assert!(root_names.contains(&"main.py"));
    assert!(root_names.contains(&"config.yaml"));
    assert!(!root_names.iter().any(|name| name.contains(".hidden")));
    assert!(!root_names.iter().any(|name| name.contains("__pycache__")));
    assert!(root_items.iter().all(|item| {
        item.source == "directory"
            && item.icon.as_deref() == Some("\u{25c7}")
            && item.id.starts_with("dir:")
    }));

    let directory_items = root_items
        .iter()
        .filter(|item| item.description.as_deref() == Some("directory"))
        .collect::<Vec<_>>();
    assert!(!directory_items.is_empty());
    assert!(directory_items
        .iter()
        .all(|item| item.completion.ends_with('/')));

    let src_items = provider.provide(&token("@src/"));
    let src_names = display_texts(&src_items);
    assert!(src_names.contains(&"src/app.py"));
    assert!(src_names.contains(&"src/utils.py"));
    assert!(src_names.contains(&"src/ui/"));

    let fragment_items = provider.provide(&token("@src/ap"));
    assert!(display_texts(&fragment_items).contains(&"src/app.py"));
    assert!(!fragment_items.is_empty());
}

fn token(text: &str) -> CompletionToken {
    CompletionToken {
        text: text.to_owned(),
        start: 0,
        end: text.len(),
        trigger: "@".to_owned(),
    }
}

fn display_texts(items: &[iac_code_tui::SuggestionItem]) -> Vec<&str> {
    items
        .iter()
        .map(|item| item.display_text.as_str())
        .collect()
}

fn create_sample_tree(root: &Path) {
    fs::write(root.join("main.py"), "# main").expect("write main");
    fs::write(root.join("config.yaml"), "key: value").expect("write config");
    fs::create_dir(root.join("src")).expect("create src");
    fs::write(root.join("src/app.py"), "# app").expect("write app");
    fs::write(root.join("src/utils.py"), "# utils").expect("write utils");
    fs::create_dir(root.join("src/ui")).expect("create ui");
    fs::write(root.join("src/ui/input.py"), "# input").expect("write input");

    fs::create_dir(root.join(".git")).expect("create git");
    fs::write(root.join(".git/config"), "git config").expect("write git config");
    fs::create_dir(root.join(".hidden")).expect("create hidden");
    fs::write(root.join(".hidden/secret.py"), "secret").expect("write hidden");
    fs::create_dir(root.join("__pycache__")).expect("create pycache");
    fs::write(root.join("__pycache__/bytecode.pyc"), "bytes").expect("write pycache");
    fs::create_dir(root.join("mypackage.egg-info")).expect("create egg-info");
    fs::write(root.join("mypackage.egg-info/PKG-INFO"), "pkg info").expect("write egg-info");
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
