use iac_code_tui::{
    draw_transcript_view, draw_transcript_view_wrapped, transcript_should_exit,
    wrap_transcript_lines, wrap_transcript_lines_tail, TranscriptSegment, TranscriptTurn,
    TranscriptViewState,
};

#[test]
fn transcript_view_renders_history_and_current_segments_like_python() {
    let state = TranscriptViewState::new(vec![
        TranscriptTurn::user("hello"),
        TranscriptTurn::assistant(vec![
            TranscriptSegment::text("answer"),
            TranscriptSegment::tool(
                vec!["HEAD".to_owned(), "  CHILD".to_owned()],
                Some("line 1\nline 2"),
                Some("RESULT"),
            ),
        ]),
    ])
    .with_current_segments(vec![TranscriptSegment::text("streaming")]);

    let lines = state.render_lines();
    let joined = lines.join("\n");

    assert!(joined.contains("❯ hello"));
    assert!(joined.contains("answer"));
    assert!(joined.contains("HEAD"));
    assert!(joined.contains("Prompt:"));
    assert!(joined.contains("line 1"));
    assert!(joined.contains("line 2"));
    assert!(joined.contains("CHILD"));
    assert!(joined.contains("RESULT"));
    assert!(joined.contains("streaming"));
}

#[test]
fn transcript_view_inserts_subagent_prompt_between_tool_header_and_children() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::tool(
            vec!["HEAD".to_owned(), "  CHILD".to_owned()],
            Some("do work"),
            Some("RESULT"),
        ),
    ])]);
    let output = state.render_lines().join("\n");

    let head_pos = output.find("HEAD").expect("header should render");
    let prompt_pos = output.find("Prompt:").expect("prompt should render");
    let child_pos = output.find("CHILD").expect("child should render");
    let result_pos = output.find("RESULT").expect("result should render");

    assert!(head_pos < prompt_pos);
    assert!(prompt_pos < child_pos);
    assert!(child_pos < result_pos);
}

#[test]
fn transcript_view_current_segments_spacing_matches_python_rules() {
    let after_user = TranscriptViewState::new(vec![TranscriptTurn::user("hello")])
        .with_current_segments(vec![TranscriptSegment::text("streaming")]);
    assert_eq!(after_user.render_lines(), vec!["❯ hello", "", "streaming"]);

    let after_assistant = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::text("answer"),
    ])])
    .with_current_segments(vec![TranscriptSegment::text("streaming")]);
    assert_eq!(after_assistant.render_lines(), vec!["answer", "streaming"]);
}

#[test]
fn transcript_view_keeps_blank_line_between_consecutive_text_segments_like_python() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::text("first"),
        TranscriptSegment::text("second"),
    ])]);

    assert_eq!(state.render_lines(), vec!["first", "", "second"]);
    assert_eq!(state.render_wrapped_lines(80), vec!["first", "", "second"]);
}

#[test]
fn transcript_view_wraps_user_and_assistant_lines_to_terminal_width() {
    let state = TranscriptViewState::new(vec![
        TranscriptTurn::user("abcdef"),
        TranscriptTurn::assistant(vec![TranscriptSegment::text("ghijkl")]),
    ]);

    assert_eq!(
        state.render_wrapped_lines(4),
        vec!["❯ ab", "  cd", "  ef", "", "ghij", "kl"]
    );
}

#[test]
fn transcript_view_wraps_tool_prompt_with_unicode_display_width() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::tool(vec!["HEAD".to_owned()], Some("你好ab界cd"), Some("RESULT")),
    ])]);

    assert_eq!(
        state.render_wrapped_lines(12),
        vec!["HEAD", "  ⎿  Prompt:", "     你好ab", "     界cd", "RESULT"]
    );
}

#[test]
fn transcript_view_uses_prefix_only_fallback_when_terminal_is_narrow() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::user("abcdef")]);

    assert_eq!(state.render_wrapped_lines(2), vec!["❯ "]);
    assert_eq!(state.render_wrapped_lines(1), vec!["❯"]);
}

#[test]
fn transcript_view_preserves_wide_chars_when_prefixed_content_column_is_too_narrow() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::user("你好ab")]);

    assert_eq!(state.render_wrapped_lines(3), vec!["❯ ", "你", "好a", "b"]);
}

#[test]
fn transcript_view_wraps_by_grapheme_cluster_display_width() {
    let family = "👨‍👩‍👧‍👦";
    let state = TranscriptViewState::new(vec![TranscriptTurn::user(format!("{family}a"))]);

    assert_eq!(state.render_wrapped_lines(5), vec![format!("❯ {family}a")]);
}

#[test]
fn transcript_view_keeps_url_tokens_intact_when_wrapping() {
    let url = "https://example.com/long-url-with-dashes/path";
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::text(format!("see {url} now")),
    ])]);

    let lines = state.render_wrapped_lines(20);

    assert_eq!(lines, vec!["see", url, "now"]);
}

#[test]
fn transcript_view_still_wraps_long_non_url_tokens() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::text("see a_very_long_non_url_token now"),
    ])]);

    let lines = state.render_wrapped_lines(10);

    assert_eq!(lines, vec!["see", "a_very_lon", "g_non_url_", "token now"]);
}

#[test]
fn transcript_view_wraps_prose_without_splitting_words_when_space_is_available() {
    let sample = "Mara found an old key on the shore. Curious, she opened a tarnished box half-buried in sand-and inside lay a single glowing seed.";
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::text(sample),
    ])]);

    let lines = state.render_wrapped_lines(40);
    let joined = lines.join("\n");

    assert!(
        !joined.contains("Curi\nous"),
        "word 'Curious' should not be split across lines:\n{joined}"
    );
    assert!(
        !joined.contains("bur\nied"),
        "word 'half-buried' should not be split across lines:\n{joined}"
    );
    assert!(
        lines
            .iter()
            .all(|line| iac_code_tui::terminal_display_width(line) <= 40),
        "wrapped prose must stay within terminal width: {lines:?}"
    );
}

#[test]
fn transcript_view_wraps_prefixed_user_prompt_without_splitting_words_when_space_is_available() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::user("alpha beta gamma delta")]);

    let lines = state.render_wrapped_lines(14);

    assert_eq!(lines, vec!["❯ alpha beta", "  gamma delta"]);
    assert!(
        lines
            .iter()
            .all(|line| iac_code_tui::terminal_display_width(line) <= 14),
        "prefixed prompt lines must stay within terminal width: {lines:?}"
    );
}

#[test]
fn transcript_view_wraps_prefixed_tool_prompt_without_splitting_words_when_space_is_available() {
    let state = TranscriptViewState::new(vec![TranscriptTurn::assistant(vec![
        TranscriptSegment::tool(
            vec!["HEAD".to_owned()],
            Some("deploy resource group safely"),
            Some("RESULT"),
        ),
    ])]);

    let lines = state.render_wrapped_lines(18);

    assert_eq!(
        lines,
        vec![
            "HEAD",
            "  ⎿  Prompt:",
            "     deploy",
            "     resource",
            "     group safely",
            "RESULT",
        ]
    );
    assert!(
        lines
            .iter()
            .all(|line| iac_code_tui::terminal_display_width(line) <= 18),
        "prefixed tool prompt lines must stay within terminal width: {lines:?}"
    );
}

#[test]
fn transcript_view_draw_crops_oldest_lines_and_renders_footer() {
    let drawn = draw_transcript_view(
        &[
            "one".to_owned(),
            "two".to_owned(),
            "three".to_owned(),
            "four".to_owned(),
            "five".to_owned(),
            "six".to_owned(),
        ],
        6,
    );

    assert!(!drawn.visible_lines.contains(&"one".to_owned()));
    assert!(!drawn.visible_lines.contains(&"two".to_owned()));
    assert_eq!(drawn.visible_lines, vec!["three", "four", "five", "six"]);
    assert_eq!(drawn.footer, "Showing transcript · ctrl+o to toggle");
}

#[test]
fn transcript_view_draw_wraps_existing_lines_before_cropping() {
    let drawn = draw_transcript_view_wrapped(&["❯ abcdef".to_owned(), "ghijkl".to_owned()], 5, 4);

    assert_eq!(drawn.visible_lines, vec!["  ef", "ghij", "kl"]);
    assert_eq!(drawn.footer, "Showing transcript · ctrl+o to toggle");
}

#[test]
fn transcript_view_tail_wrap_matches_full_wrap_suffix() {
    let lines = vec![
        "old transcript line that should fall out of the viewport".to_owned(),
        "❯ user prompt wraps with a prefixed continuation".to_owned(),
        "assistant response keeps ordinary words together when possible".to_owned(),
        "     tool prompt uses the continuation prefix".to_owned(),
        "final line stays visible".to_owned(),
    ];

    let full = wrap_transcript_lines(&lines, 12);
    let expected = full[full.len() - 6..].to_vec();

    assert_eq!(wrap_transcript_lines_tail(&lines, 12, 6), expected);
}

#[test]
fn transcript_view_exit_shortcuts_match_python() {
    assert!(transcript_should_exit("o", true));
    assert!(transcript_should_exit("c", true));
    assert!(transcript_should_exit("escape", false));
    assert!(!transcript_should_exit("enter", false));
}
