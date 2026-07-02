use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::{build_global_search_preview, parse_global_search_results, GlobalSearchItem};

#[test]
fn global_search_parses_rg_or_grep_output_like_python() {
    let root = unique_temp_dir("iac-code-rs-global-search-parse");
    let main_py = root.join("main.py");
    let utils_py = root.join("utils.py");
    let output = format!(
        "{}:3:hello world\n{}:10: hello again \n",
        main_py.display(),
        utils_py.display()
    );

    let items = parse_global_search_results(&root, &output);

    assert_eq!(items.len(), 2);
    assert_eq!(items[0].key, format!("{}:3", main_py.display()));
    assert_eq!(items[0].display, "main.py:3  hello world");
    assert_eq!(items[0].file_path, main_py);
    assert_eq!(items[0].line_number, 3);
    assert_eq!(items[0].text, "hello world");
    assert_eq!(items[0].filter_text, "main.py:3  hello world");
    assert_eq!(items[0].selection_insert_text(), "@main.py:3  hello world");
    assert_eq!(items[1].display, "utils.py:10  hello again");

    fs::remove_dir_all(root).ok();
}

#[test]
fn global_search_skips_invalid_lines_dedupes_and_handles_colons_in_paths() {
    let root = unique_temp_dir("iac-code-rs-global-search-edge");
    let colon_path = root.join("dir:name").join("main.py");
    let duplicate = format!("{}:7:hello world\n", colon_path.display());
    let output = format!(
        "not enough fields\n{}:abc:bad lineno\n{}{}:8:second hit\n",
        root.join("main.py").display(),
        duplicate,
        colon_path.display()
    );

    let items = parse_global_search_results(&root, &output);

    assert_eq!(items.len(), 2);
    assert_eq!(items[0].key, format!("{}:7", colon_path.display()));
    assert_eq!(items[0].display, "dir:name/main.py:7  hello world");
    assert_eq!(items[1].key, format!("{}:8", colon_path.display()));

    fs::remove_dir_all(root).ok();
}

#[test]
fn global_search_preview_reads_matched_line_window_and_title() {
    let root = unique_temp_dir("iac-code-rs-global-search-preview");
    let src = root.join("src");
    fs::create_dir_all(&src).expect("fixture directory should be created");
    let file = src.join("main.py");
    let content = (1..=20)
        .map(|line| format!("line-{line}\n"))
        .collect::<String>();
    fs::write(&file, content).expect("fixture file should be written");
    let item = GlobalSearchItem::new(file.clone(), &root, 10, "line-10");

    let preview = build_global_search_preview(&root, &item);

    assert_eq!(preview.title, "src/main.py:10");
    assert_eq!(preview.language, "py");
    assert_eq!(preview.start_line, 5);
    assert_eq!(preview.highlight_line, 10);
    assert!(preview.content.starts_with("line-5\n"));
    assert!(preview.content.contains("line-10\n"));
    assert!(preview.content.ends_with("line-15\n"));

    fs::remove_dir_all(root).ok();
}

#[test]
fn global_search_preview_clips_near_start_uses_text_for_no_extension_and_handles_missing_file() {
    let root = unique_temp_dir("iac-code-rs-global-search-preview-edge");
    let makefile = root.join("Makefile");
    fs::write(&makefile, "all:\n\techo done\n").expect("fixture file should be written");
    let item = GlobalSearchItem::new(makefile.clone(), &root, 1, "all:");

    let preview = build_global_search_preview(&root, &item);

    assert_eq!(preview.title, "Makefile:1");
    assert_eq!(preview.language, "text");
    assert_eq!(preview.start_line, 1);
    assert_eq!(preview.highlight_line, 1);
    assert_eq!(preview.content, "all:\n\techo done\n");

    let missing = GlobalSearchItem::new(root.join("missing.py"), &root, 1, "hello");
    let missing_preview = build_global_search_preview(&root, &missing);
    assert_eq!(missing_preview.content, "");
    assert_eq!(missing_preview.language, "py");

    fs::remove_dir_all(root).ok();
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
