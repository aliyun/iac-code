use std::time::Duration;

use iac_code_protocol::json;
use iac_code_protocol::{
    MessageEndEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolResultEvent,
    ToolUseEndEvent, Usage,
};

use crate::cli_i18n::tr_compaction_result;
use crate::interactive_commands::interactive_compact_status;
use crate::interactive_renderer::{InteractiveEventRenderer, INTERACTIVE_LIVE_THINKING_MAX_ROWS};
use crate::interactive_working::{
    format_spinner_elapsed, interactive_working_frame, interactive_working_frame_inline,
    interactive_working_pause_clear_sequence, WorkingIndicatorState,
};
use crate::test_support::{
    english_locale_guard, raw_ansi_screen_after_writes, raw_strip_ansi_sequences, EnvVarGuard,
};

#[test]
fn interactive_compaction_result_reports_reduction_direction() {
    let _locale = english_locale_guard();

    assert_eq!(
        tr_compaction_result(900, 300, "0%"),
        "Context compacted: 900 \u{2192} 300 tokens (67% reduction). Context usage: 0%"
    );
}

#[test]
fn interactive_compaction_result_reports_increase_direction() {
    let _locale = english_locale_guard();

    let message = tr_compaction_result(209, 267, "0%");

    assert_eq!(
        message,
        "Context compacted: 209 \u{2192} 267 tokens (28% increase). Context usage: 0%"
    );
    assert!(!message.contains("-28%"));
    assert!(!message.contains("reduction"));
}

#[test]
fn interactive_compact_uses_specific_working_status() {
    let _locale = english_locale_guard();

    assert_eq!(interactive_compact_status(), "Compacting context");
}

#[test]
fn interactive_event_renderer_groups_tool_results_with_their_headers_like_python() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::from_secs(6));
    for event in [
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: "read-a".into(),
            name: "read_file".into(),
            input: json::object([("path", json::string("a.md"))]),
        }),
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: "read-b".into(),
            name: "read_file".into(),
            input: json::object([("path", json::string("b.md"))]),
        }),
        StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "tool_use".into(),
            usage: Usage {
                input_tokens: 5,
                output_tokens: 7,
                ..Usage::default()
            },
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "read-a".into(),
            tool_name: "read_file".into(),
            result: "a.md (3 lines)\nalpha\nbeta\n".into(),
            is_error: false,
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "read-b".into(),
            tool_name: "read_file".into(),
            result: "b.md (4 lines)\none\ntwo\n".into(),
            is_error: false,
        }),
        StreamEvent::TextDelta(TextDeltaEvent {
            text: "Done".into(),
        }),
    ] {
        renderer.push_event(&event);
    }

    let output = raw_strip_ansi_sequences(&renderer.finish());
    let read_a = output.find("● Read(a.md)").expect("read a header");
    let result_a = output.find("⎿  Read 3 lines").expect("read a result");
    let read_b = output.find("● Read(b.md)").expect("read b header");
    let result_b = output.find("⎿  Read 4 lines").expect("read b result");
    let usage = output.find("5 input · 7 output").expect("usage line");
    let done = output.find("Done").expect("assistant text");
    let processed = output.find("✱ Processed").expect("processed status");

    assert!(
        read_a < result_a && result_a < read_b && read_b < result_b,
        "{output}"
    );
    assert!(
        result_b < usage && usage < done && done < processed,
        "{output}"
    );
    assert!(output.contains("(ctrl+o to expand)"), "{output}");
    assert!(!output.contains("✱ Processed 5 input"), "{output}");
}

#[test]
fn interactive_event_renderer_styles_tool_dot_without_coloring_label_like_python() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::ZERO);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "write-1".into(),
        name: "write_file".into(),
        input: json::object([("path", json::string("out.txt"))]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "write-1".into(),
        tool_name: "write_file".into(),
        result: "Successfully wrote 1 lines to out.txt".into(),
        is_error: false,
    }));

    let output = renderer.finish();

    assert!(
        output.contains("\x1b[32m● \x1b[0m\x1b[1mWrite(out.txt)\x1b[0m"),
        "{output:?}"
    );
    assert!(
        !output.contains("\x1b[32m● Write(out.txt)\x1b[0m"),
        "{output:?}"
    );
}

#[test]
fn interactive_event_renderer_summarizes_list_files_like_python() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::ZERO);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "list-1".into(),
        name: "list_files".into(),
        input: json::object([("path", json::string("src"))]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "list-1".into(),
        tool_name: "list_files".into(),
        result: "Directory: src\n\n  main.rs (10B)\n  lib.rs (20B)\n  nested/".into(),
        is_error: false,
    }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(output.contains("● List(src)"), "{output}");
    assert!(output.contains("⎿  Found 3 items"), "{output}");
    assert!(!output.contains("Directory: src"), "{output}");
}

#[test]
fn interactive_event_renderer_summarizes_aliyun_api_like_python() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::ZERO);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "aliyun-1".into(),
        name: "aliyun_api".into(),
        input: json::object([
            ("product", json::string("ros")),
            ("action", json::string("ListStacks")),
            ("region_id", json::string("cn-beijing")),
        ]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "aliyun-1".into(),
            tool_name: "aliyun_api".into(),
            result: "HTTP error 400 Response: {\"RequestId\":\"REQ-42\",\"Code\":\"InvalidAccessKeyId.Inactive\",\"Message\":\"Specified access key is disabled.\"}".into(),
            is_error: true,
        }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(
        output.contains("● Aliyun API(ListStacks ros cn-beijing)"),
        "{output}"
    );
    assert!(
            output.contains(
                "⎿  Error: InvalidAccessKeyId.Inactive code: 400, Specified access key is disabled. request id: REQ-42"
            ),
            "{output}"
        );
    assert!(!output.contains("Response:"), "{output}");
}

#[test]
fn interactive_event_renderer_summarizes_aliyun_api_success_request_id() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::ZERO);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "aliyun-1".into(),
        name: "aliyun_api".into(),
        input: json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            ("region_id", json::string("cn-hangzhou")),
        ]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "aliyun-1".into(),
        tool_name: "aliyun_api".into(),
        result: "{\n  \"RequestId\": \"REQ-99\",\n  \"Instances\": []\n}".into(),
        is_error: false,
    }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(
        output.contains("● Aliyun API(DescribeInstances ecs cn-hangzhou)"),
        "{output}"
    );
    assert!(
        output.contains("⎿  Call succeeded (RequestId: REQ-99)"),
        "{output}"
    );
    assert!(!output.contains("\"Instances\""), "{output}");
}

#[test]
fn interactive_event_renderer_localizes_usage_and_read_ranges_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let mut renderer = InteractiveEventRenderer::new(Duration::ZERO);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "read-1".into(),
        name: "read_file".into(),
        input: json::object([
            ("path", json::string("/workspace/iac-code/README.md")),
            ("start_line", json::number(1)),
            ("end_line", json::number(80)),
        ]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "read-1".into(),
        tool_name: "read_file".into(),
        result: "File: /workspace/iac-code/README.md (lines 1-80 of 628)\n# iac-code\n".into(),
        is_error: false,
    }));
    renderer.push_event(&StreamEvent::MessageEnd(MessageEndEvent {
        stop_reason: "tool_use".into(),
        usage: Usage {
            input_tokens: 7_000,
            output_tokens: 454,
            cache_read_input_tokens: 4_300,
            ..Usage::default()
        },
    }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(output.contains("● 读取(第 1-80 行)"), "{output}");
    assert!(output.contains("⎿  第 1-80 行（共 628 行）"), "{output}");
    assert!(
        output.contains("7k 输入 · 454 输出 · 4.3k 缓存读取"),
        "{output}"
    );
    assert!(!output.contains("/Users/prodesire/projects"), "{output}");
    assert!(!output.contains("input ·"), "{output}");
    assert!(!output.contains("cache_read"), "{output}");
}

#[test]
fn interactive_event_renderer_streams_thinking_content_then_summary_like_python_live() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);

    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "I should inspect files first.\n".into(),
    }));
    let thinking_output = raw_strip_ansi_sequences(&renderer.take_output());
    assert!(
        thinking_output.starts_with("\n▌ "),
        "live thinking should keep a blank line before the thinking block: {thinking_output:?}"
    );
    assert!(
        thinking_output.contains("▌ I should inspect files first."),
        "{thinking_output}"
    );

    renderer.push_event(&StreamEvent::TextDelta(TextDeltaEvent {
        text: "Done\n".into(),
    }));
    let final_output = raw_strip_ansi_sequences(&renderer.finish());
    assert!(final_output.contains("▌ Thought for"), "{final_output}");
    assert!(final_output.contains("✦ Done"), "{final_output}");
    assert!(
        !final_output.contains("I should inspect files first."),
        "{final_output}"
    );
}

#[cfg(unix)]
#[test]
fn interactive_event_renderer_live_thinking_clears_wrapped_rows_without_growing_blank_gap() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("COLUMNS", "16"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);

    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "this reasoning line is long enough to wrap".into(),
    }));
    let first = renderer.take_output();
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: " and it keeps growing".into(),
    }));
    let second = renderer.take_output();

    let screen = raw_ansi_screen_after_writes(16, 8, &[first.as_bytes(), second.as_bytes()]);
    let first_non_empty = screen
        .lines
        .iter()
        .position(|line| !line.trim().is_empty())
        .expect("live thinking should leave visible content");

    assert_eq!(
        first_non_empty, 1,
        "live thinking should keep exactly one blank row before the transient block: {screen:?}"
    );
    assert!(
        screen.lines[..first_non_empty]
            .iter()
            .all(|line| line.trim().is_empty()),
        "rows before the transient thinking block should stay blank only: {screen:?}"
    );
}

#[cfg(unix)]
#[test]
fn interactive_event_renderer_live_thinking_deduplicates_cumulative_deltas() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
        ("COLUMNS", "120"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);

    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "用户要求".into(),
    }));
    let first = renderer.take_output();
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "用户要求我识别图片中的".into(),
    }));
    let second = renderer.take_output();
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "用户要求我识别图片中的文字。图片中显示了两行中文字符".into(),
    }));
    let third = renderer.take_output();

    let screen = raw_ansi_screen_after_writes(
        120,
        10,
        &[first.as_bytes(), second.as_bytes(), third.as_bytes()],
    );
    let visible = screen
        .lines
        .iter()
        .filter(|line| !line.trim().is_empty())
        .cloned()
        .collect::<Vec<_>>();

    assert_eq!(
        visible,
        vec!["▌ 用户要求我识别图片中的文字。图片中显示了两行中文字符"],
        "cumulative thinking deltas should replace prior prefixes: {screen:?}"
    );
}

#[cfg(unix)]
#[test]
fn interactive_event_renderer_live_thinking_clears_after_working_indicator_resume() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("COLUMNS", "120"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);

    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "The user is asking me".into(),
    }));
    let first = renderer.take_output();
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: " to read the text in an image.".into(),
    }));
    let second = renderer.take_output();
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: " Let me look at the image that was provided.".into(),
    }));
    let third = renderer.take_output();

    let screen = raw_ansi_screen_after_writes(
        120,
        12,
        &[
            first.as_bytes(),
            interactive_working_frame("Working", "⠋", Duration::from_secs(1)).as_bytes(),
            interactive_working_pause_clear_sequence(true).as_bytes(),
            second.as_bytes(),
            interactive_working_frame("Working", "⠙", Duration::from_secs(2)).as_bytes(),
            interactive_working_pause_clear_sequence(true).as_bytes(),
            third.as_bytes(),
        ],
    );
    let visible = screen
        .lines
        .iter()
        .filter(|line| !line.trim().is_empty())
        .cloned()
        .collect::<Vec<_>>();

    assert_eq!(
            visible,
            vec![
                "▌ The user is asking me to read the text in an image. Let me look at the image that was provided."
            ],
            "working indicator resume should not leave stale live thinking rows: {screen:?}"
        );
}

#[cfg(unix)]
#[test]
fn interactive_event_renderer_live_thinking_survives_inline_spinner_frames() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("COLUMNS", "120"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);
    let mut indicator = WorkingIndicatorState::default();
    let mut writes: Vec<Vec<u8>> = Vec::new();

    // Reproduce the real interactive loop: a free-running spinner whose first
    // frame opens the live region with a leading blank line, then repaints
    // inline in place, with the renderer pausing/clearing between deltas.
    // The inline frames are the case the earlier test missed — they leave
    // the blank on screen, so the pause must still step back over it.
    let spinner_frame = |indicator: &mut WorkingIndicatorState, secs: u64| -> Vec<u8> {
        if indicator.next_frame_uses_leading_blank() {
            interactive_working_frame("Working", "⠋", Duration::from_secs(secs)).into_bytes()
        } else {
            interactive_working_frame_inline("Working", "⠋", Duration::from_secs(secs)).into_bytes()
        }
    };

    // Spinner ticks before the first delta arrives.
    writes.push(spinner_frame(&mut indicator, 1)); // opens with leading blank
    writes.push(spinner_frame(&mut indicator, 1)); // inline repaint

    for (index, delta) in [
        "The user is asking about",
        " the text in the image. Let me read it directly",
    ]
    .into_iter()
    .enumerate()
    {
        // Sink order: pause+clear, paint the renderer output, resume.
        writes.push(
            interactive_working_pause_clear_sequence(indicator.take_leading_blank()).into_bytes(),
        );
        renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: delta.into(),
        }));
        writes.push(renderer.take_output().into_bytes());
        indicator.needs_leading_blank = true; // resume()

        // Spinner ticks again before the next delta / end of stream.
        let secs = index as u64 * 2 + 2;
        writes.push(spinner_frame(&mut indicator, secs)); // leading blank
        writes.push(spinner_frame(&mut indicator, secs)); // inline repaint
    }

    // End of stream: stop the indicator (clearing the live region) then flush.
    writes.push(
        interactive_working_pause_clear_sequence(indicator.take_leading_blank()).into_bytes(),
    );
    writes.push(renderer.finish().into_bytes());

    let byte_writes: Vec<&[u8]> = writes.iter().map(Vec::as_slice).collect();
    let screen = raw_ansi_screen_after_writes(120, 16, &byte_writes);
    let visible = screen
        .lines
        .iter()
        .filter(|line| !line.trim().is_empty())
        .cloned()
        .collect::<Vec<_>>();

    assert!(
        visible.iter().all(|line| !line.contains("asking about")),
        "live thinking snapshots must be cleared, not orphaned across inline frames: {screen:?}"
    );
    assert!(
        visible.iter().any(|line| line.contains("Thought for")),
        "the collapsed thought summary should remain after the stream ends: {screen:?}"
    );
}

#[test]
fn live_thinking_render_due_throttles_to_the_min_interval() {
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);
    renderer.live_thinking_min_interval = Duration::from_secs(3600);

    // The first repaint of a block always fires.
    assert!(renderer.live_thinking_render_due());
    // A second repaint within the interval is suppressed so the spinner is
    // not paused/redrawn on every delta (the flicker fix).
    assert!(!renderer.live_thinking_render_due());

    // A zero interval (the default in tests and non-live paths) never
    // throttles, so every delta still renders.
    renderer.live_thinking_min_interval = Duration::ZERO;
    assert!(renderer.live_thinking_render_due());
    assert!(renderer.live_thinking_render_due());
}

#[cfg(unix)]
#[test]
fn live_thinking_preview_crops_to_the_trailing_rows() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("COLUMNS", "120"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);
    let thinking = (1..=20)
        .map(|n| format!("reasoning line {n}"))
        .collect::<Vec<_>>()
        .join("\n");
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: thinking,
    }));
    let output = raw_strip_ansi_sequences(&renderer.take_output());
    let preview_rows = output
        .lines()
        .filter(|line| line.contains("▌ reasoning line"))
        .count();

    assert!(
            preview_rows <= INTERACTIVE_LIVE_THINKING_MAX_ROWS,
            "preview should be cropped to at most {INTERACTIVE_LIVE_THINKING_MAX_ROWS} rows, got {preview_rows}: {output:?}"
        );
    // It keeps the most recent reasoning, not the oldest.
    assert!(output.contains("▌ reasoning line 20"), "{output:?}");
    assert!(output.contains("▌ reasoning line 15"), "{output:?}");
    assert!(!output.contains("▌ reasoning line 14"), "{output:?}");
}

#[test]
fn interactive_event_renderer_separates_usage_and_processed_status_like_python() {
    let _locale = english_locale_guard();
    let mut renderer = InteractiveEventRenderer::new(Duration::from_secs(3));
    renderer.push_event(&StreamEvent::TextDelta(TextDeltaEvent {
        text: "Done\n".into(),
    }));
    renderer.push_event(&StreamEvent::MessageEnd(MessageEndEvent {
        stop_reason: "stop".into(),
        usage: Usage {
            input_tokens: 8,
            output_tokens: 13,
            ..Usage::default()
        },
    }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(
        output.contains("✦ Done\n\n  8 input · 13 output\n\n✱ Processed 3.0s\n\n"),
        "usage and processed status should be visually separated like Python: {output:?}"
    );
}

#[test]
fn interactive_working_frame_inline_repaints_current_status_line() {
    let output = interactive_working_frame_inline("Working", "⠙", Duration::from_secs(11));

    assert!(
        output.starts_with("\r\x1b[2K"),
        "inline working frame should repaint the current live status row: {output:?}"
    );
    assert!(!output.starts_with('\n'), "{output:?}");
}

#[test]
fn working_indicator_state_keeps_leading_blank_through_inline_frames() {
    let mut state = WorkingIndicatorState::default();

    // The first frame opens the live region with a leading blank line.
    assert!(state.next_frame_uses_leading_blank());
    // Inline repaints leave that blank on screen, so they must NOT clear
    // the bookkeeping (the regression that orphaned thinking snapshots).
    assert!(!state.next_frame_uses_leading_blank());
    assert!(!state.next_frame_uses_leading_blank());
    // A pause therefore still steps the cursor back up over the blank.
    assert!(state.take_leading_blank());
    // Once consumed, a second clear without an intervening frame is a no-op.
    assert!(!state.take_leading_blank());

    // After `resume` requests it, the next frame re-establishes the blank.
    state.needs_leading_blank = true;
    assert!(state.next_frame_uses_leading_blank());
    assert!(!state.next_frame_uses_leading_blank());
    assert!(state.take_leading_blank());
}

#[cfg(unix)]
#[test]
fn interactive_event_renderer_keeps_blank_line_between_usage_and_final_thinking_summary() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("COLUMNS", "120"),
    ]);
    let mut renderer = InteractiveEventRenderer::streaming_with_live_updates(true);
    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "read-1".into(),
        name: "read_file".into(),
        input: json::object([("path", json::string("README.md"))]),
    }));
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "read-1".into(),
        tool_name: "read_file".into(),
        result: "README.md (76 lines)\n# title\n".into(),
        is_error: false,
    }));
    renderer.push_event(&StreamEvent::MessageEnd(MessageEndEvent {
        stop_reason: "tool_use".into(),
        usage: Usage {
            input_tokens: 4_900,
            output_tokens: 101,
            ..Usage::default()
        },
    }));
    renderer.push_event(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: "I should summarize what I found.".into(),
    }));
    let live_output = renderer.take_output();

    renderer.push_event(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: "list-1".into(),
        name: "list_files".into(),
        input: json::object([("path", json::string("src"))]),
    }));
    let thinking_summary_output = renderer.take_output();
    renderer.push_event(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "list-1".into(),
        tool_name: "list_files".into(),
        result: "Found 19 items".into(),
        is_error: false,
    }));
    let tool_output = renderer.take_output();

    let screen = raw_ansi_screen_after_writes(
        120,
        16,
        &[
            interactive_working_frame("Working", "⠋", Duration::ZERO).as_bytes(),
            b"\r\x1b[2K",
            live_output.as_bytes(),
            interactive_working_frame_inline("Working", "⠙", Duration::from_secs(1)).as_bytes(),
            b"\r\x1b[2K",
            thinking_summary_output.as_bytes(),
            interactive_working_frame_inline("Working", "⠹", Duration::from_secs(2)).as_bytes(),
            b"\r\x1b[2K",
            tool_output.as_bytes(),
        ],
    );
    let usage_row = screen
        .lines
        .iter()
        .position(|line| line.contains("4.9k input · 101 output"))
        .expect("usage line should be visible");
    let thinking_row = screen
        .lines
        .iter()
        .position(|line| line.contains("▌ Thought for"))
        .expect("final thinking summary should be visible");

    assert_eq!(
        thinking_row,
        usage_row + 2,
        "final thinking summary should keep one blank row after usage: {screen:?}"
    );
    assert!(
        screen.lines[usage_row + 1].trim().is_empty(),
        "row between usage and final thinking summary should be blank: {screen:?}"
    );
    let tool_row = screen
        .lines
        .iter()
        .position(|line| line.contains("● List(src)"))
        .unwrap_or_else(|| panic!("next tool header should be visible: {screen:?}"));
    assert_eq!(
        tool_row,
        thinking_row + 2,
        "next tool header should keep one blank row after final thinking summary: {screen:?}"
    );
}

#[test]
fn interactive_event_renderer_keeps_streamed_markdown_tables_intact() {
    let mut renderer = InteractiveEventRenderer::streaming();
    renderer.push_event(&StreamEvent::TextDelta(TextDeltaEvent {
        text: "## Project\n\n| Module | Responsibility |\n".into(),
    }));
    assert!(
        renderer.take_output().is_empty(),
        "table should not be rendered before the markdown block is complete"
    );
    renderer.push_event(&StreamEvent::TextDelta(TextDeltaEvent {
        text: "| --- | --- |\n| cli | Entry point |\n\nDone\n".into(),
    }));

    let output = raw_strip_ansi_sequences(&renderer.finish());

    assert!(output.contains("Project"), "{output}");
    assert!(
        !output.contains("✦ Project"),
        "markdown headings should not be prefixed as assistant prose: {output}"
    );
    assert!(
        !output.contains("✦ Done"),
        "markdown document continuations should not restart assistant bullets: {output}"
    );
    assert!(output.contains("Module"), "{output}");
    assert!(output.contains("Responsibility"), "{output}");
    assert!(!output.contains("| Module | Responsibility |"), "{output}");
    assert!(!output.contains("## Project"), "{output}");
}

#[test]
fn interactive_working_frame_starts_after_blank_line_like_python_live_status() {
    let frame = interactive_working_frame("Working", "⠋", Duration::from_secs(2));

    assert!(
        frame.starts_with("\n\r\x1b[2K"),
        "working status should leave a blank line before the transient frame: {frame:?}"
    );
}

#[test]
fn interactive_working_elapsed_matches_python_spinner_format() {
    assert_eq!(format_spinner_elapsed(Duration::from_secs(7)), "7s");
    assert_eq!(format_spinner_elapsed(Duration::from_secs(62)), "1m 02s");
}
