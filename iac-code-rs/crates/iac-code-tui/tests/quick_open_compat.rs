use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::{build_quick_open_items, build_quick_open_preview, QuickOpenItem};

#[test]
fn quick_open_builds_file_items_with_relative_display_and_absolute_metadata() {
    let root = unique_temp_dir("iac-code-rs-quick-open-items");
    fs::write(root.join("main.py"), "print('hello')").expect("fixture should be written");
    fs::write(root.join("utils.py"), "def helper(): pass").expect("fixture should be written");
    let subdir = root.join("subdir");
    fs::create_dir_all(&subdir).expect("subdir should be created");
    fs::write(subdir.join("module.py"), "x = 1").expect("fixture should be written");

    let items = build_quick_open_items(&root);
    let rel_paths = items
        .iter()
        .map(|item| item.display.as_str())
        .collect::<BTreeSet<_>>();

    assert!(rel_paths.contains("main.py"));
    assert!(rel_paths.contains("utils.py"));
    assert!(rel_paths.contains("subdir/module.py"));
    let main = find_item(&items, "main.py");
    assert_eq!(main.key, "file:main.py");
    assert_eq!(main.filter_text, "main.py");
    assert!(main.file_path.is_absolute());
    assert_eq!(main.file_path, root.join("main.py"));
    assert_eq!(main.selection_insert_text(), "@main.py");

    fs::remove_dir_all(root).ok();
}

#[test]
fn quick_open_excludes_python_and_vcs_cache_directories_like_python_provider() {
    let root = unique_temp_dir("iac-code-rs-quick-open-excludes");
    fs::write(root.join("main.py"), "x").expect("fixture should be written");
    fs::create_dir_all(root.join(".git")).expect("git dir should be created");
    fs::write(root.join(".git").join("config"), "bare = false").expect("fixture should be written");
    fs::create_dir_all(root.join("__pycache__")).expect("cache dir should be created");
    fs::write(root.join("__pycache__").join("main.pyc"), "").expect("fixture should be written");
    fs::create_dir_all(root.join("pkg.egg-info")).expect("egg-info dir should be created");
    fs::write(root.join("pkg.egg-info").join("PKG-INFO"), "").expect("fixture should be written");

    let items = build_quick_open_items(&root);
    let rel_paths = items
        .iter()
        .map(|item| item.display.as_str())
        .collect::<Vec<_>>();

    assert_eq!(rel_paths, vec!["main.py"]);
    assert!(!rel_paths.iter().any(|path| path.contains(".git")));
    assert!(!rel_paths.iter().any(|path| path.contains("__pycache__")));
    assert!(!rel_paths.iter().any(|path| path.contains(".egg-info")));

    fs::remove_dir_all(root).ok();
}

#[test]
fn quick_open_preview_reads_first_20_lines_and_uses_display_as_title() {
    let root = unique_temp_dir("iac-code-rs-quick-open-preview");
    let file = root.join("big.txt");
    let content = (0..30)
        .map(|line| format!("line{line}\n"))
        .collect::<String>();
    fs::write(&file, content).expect("fixture should be written");
    let item = QuickOpenItem::new("big.txt", file);

    let preview = build_quick_open_preview(&item);

    assert_eq!(preview.title, "big.txt");
    assert_eq!(preview.language, "txt");
    assert!(preview.content.starts_with("line0\n"));
    assert!(preview.content.contains("line19\n"));
    assert!(!preview.content.contains("line20\n"));

    fs::remove_dir_all(root).ok();
}

#[test]
fn quick_open_preview_handles_missing_files_and_extensionless_paths() {
    let root = unique_temp_dir("iac-code-rs-quick-open-preview-edge");
    let makefile = root.join("Makefile");
    fs::write(&makefile, "all:\n\techo done\n").expect("fixture should be written");
    let item = QuickOpenItem::new("Makefile", makefile);

    let preview = build_quick_open_preview(&item);

    assert_eq!(preview.title, "Makefile");
    assert_eq!(preview.language, "text");
    assert_eq!(preview.content, "all:\n\techo done\n");

    let missing = QuickOpenItem::new("missing.py", root.join("missing.py"));
    let missing_preview = build_quick_open_preview(&missing);
    assert_eq!(missing_preview.content, "");
    assert_eq!(missing_preview.language, "py");

    fs::remove_dir_all(root).ok();
}

fn find_item<'a>(items: &'a [QuickOpenItem], display: &str) -> &'a QuickOpenItem {
    items
        .iter()
        .find(|item| item.display == display)
        .expect("item should exist")
}

fn unique_temp_dir(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock should be after epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("{name}-{nanos}"));
    fs::create_dir_all(&path).expect("temp directory should be created");
    path
}
