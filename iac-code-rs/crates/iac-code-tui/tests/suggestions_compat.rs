use std::cell::RefCell;
use std::rc::Rc;

use iac_code_tui::{
    CompletionToken, SuggestionAggregator, SuggestionItem, SuggestionProvider, TokenExtractor,
    OVERLAY_MAX_ITEMS,
};

#[test]
fn token_extractor_matches_python_trigger_rules() {
    let extractor = TokenExtractor::new();

    assert_token(extractor.extract("/mod", 99), "/mod", 0, 4, "/");
    assert_token(extractor.extract("\t/cmd", 5), "/cmd", 1, 5, "/");

    let slash_with_args = "run /memory delete role";
    assert_token(
        extractor.extract(slash_with_args, slash_with_args.len()),
        "/memory delete role",
        4,
        slash_with_args.len(),
        "/",
    );
    assert!(extractor.extract("src/ui", "src/ui".len()).is_none());

    assert_token(extractor.extract("@src/ui", 7), "@src/ui", 0, 7, "@");
    assert_token(extractor.extract("look @config", 12), "@config", 5, 12, "@");

    assert_token(extractor.extract("run $deploy", 11), "$deploy", 4, 11, "$");
    assert!(extractor.extract("cost$5", 6).is_none());

    assert_token(extractor.extract("!git", 4), "!git", 0, 4, "!");
    assert!(extractor.extract("echo !git", 9).is_none());
    assert!(extractor.extract("", 0).is_none());
    assert!(extractor.extract("/mod", 0).is_none());
}

#[test]
fn aggregator_dispatches_sorts_and_exposes_visible_window() {
    let slash_calls = Rc::new(RefCell::new(Vec::new()));
    let at_calls = Rc::new(RefCell::new(Vec::new()));
    let mut aggregator = SuggestionAggregator::new(vec![
        Box::new(StubProvider::new(
            "/",
            vec![
                item("low", "/low ", 1.0, None),
                item("top", "/top ", 9.0, None),
                item("mid", "/mid ", 5.0, None),
                item("four", "/four ", 4.0, None),
                item("three", "/three ", 3.0, None),
                item("two", "/two ", 2.0, None),
            ],
            Rc::clone(&slash_calls),
        )),
        Box::new(StubProvider::new(
            "@",
            vec![item("file", "@file", 10.0, None)],
            Rc::clone(&at_calls),
        )),
    ]);

    aggregator.update("/", 1);

    assert_eq!(*slash_calls.borrow(), vec!["/"]);
    assert!(at_calls.borrow().is_empty());
    assert_eq!(
        ids(aggregator.suggestions()),
        vec!["top", "mid", "four", "three", "two", "low"]
    );
    assert_eq!(
        ids(aggregator.visible_suggestions()),
        vec!["top", "mid", "four", "three", "two"]
    );
    assert_eq!(aggregator.selected_index(), 0);
    assert_eq!(aggregator.visible_selected_index(), 0);
    assert!(!aggregator.has_more_above());
    assert!(aggregator.has_more_below());

    for _ in 0..OVERLAY_MAX_ITEMS {
        aggregator.move_selection(1);
    }

    assert_eq!(aggregator.selected_index(), 5);
    assert_eq!(
        ids(aggregator.visible_suggestions()),
        vec!["mid", "four", "three", "two", "low"]
    );
    assert_eq!(aggregator.visible_selected_index(), 4);
    assert!(aggregator.has_more_above());
    assert!(!aggregator.has_more_below());

    aggregator.move_selection(1);
    assert_eq!(aggregator.selected_index(), 0);
}

#[test]
fn aggregator_accepts_selected_item_and_clears_state() {
    let mut aggregator = SuggestionAggregator::new(vec![Box::new(StubProvider::new(
        "/",
        vec![item("model", "/model ", 10.0, None)],
        Rc::new(RefCell::new(Vec::new())),
    ))]);

    aggregator.update("/mod", 4);

    assert_eq!(
        aggregator.accept_selected(),
        Some(("/model ".to_owned(), 0, 4))
    );
    assert!(aggregator.suggestions().is_empty());
    assert_eq!(aggregator.selected_index(), 0);
    assert_eq!(aggregator.ghost_text(), "");
}

#[test]
fn aggregator_ghost_text_is_case_insensitive_and_appends_arg_hint_only_for_display() {
    let mut aggregator = SuggestionAggregator::new(vec![Box::new(StubProvider::new(
        "/",
        vec![item("debug", "/debug ", 10.0, Some("[on|off]"))],
        Rc::new(RefCell::new(Vec::new())),
    ))]);

    aggregator.update("/DEB", 4);
    assert_eq!(aggregator.ghost_text(), "ug ");

    aggregator.update("/debug", 6);
    assert_eq!(aggregator.ghost_text(), " [on|off]");
    assert_eq!(
        aggregator.accept_ghost_text(),
        Some(("/debug ".to_owned(), 0, 6))
    );
}

#[test]
fn aggregator_ghost_text_handles_non_ascii_completion_boundaries() {
    let completion = format!("@{}clair", '\u{e9}');
    let mut aggregator = SuggestionAggregator::new(vec![Box::new(StubProvider::new(
        "@",
        vec![item("accented", &completion, 10.0, None)],
        Rc::new(RefCell::new(Vec::new())),
    ))]);

    aggregator.update("@e", 2);

    assert_eq!(aggregator.ghost_text(), "");
}

#[test]
fn aggregator_dismisses_when_no_trigger_or_no_matching_provider() {
    let mut aggregator = SuggestionAggregator::new(vec![Box::new(StubProvider::new(
        "/",
        vec![item("model", "/model ", 10.0, None)],
        Rc::new(RefCell::new(Vec::new())),
    ))]);

    aggregator.update("/mod", 4);
    assert!(!aggregator.suggestions().is_empty());

    aggregator.update("plain text", 10);
    assert!(aggregator.suggestions().is_empty());

    aggregator.update("@file", 5);
    assert!(aggregator.suggestions().is_empty());
    assert_eq!(aggregator.accept_selected(), None);
    assert_eq!(aggregator.accept_ghost_text(), None);
}

fn assert_token(
    token: Option<CompletionToken>,
    expected_text: &str,
    expected_start: usize,
    expected_end: usize,
    expected_trigger: &str,
) {
    let token = token.expect("token should be extracted");
    assert_eq!(token.text, expected_text);
    assert_eq!(token.start, expected_start);
    assert_eq!(token.end, expected_end);
    assert_eq!(token.trigger, expected_trigger);
}

fn item(id: &str, completion: &str, score: f64, arg_hint: Option<&str>) -> SuggestionItem {
    SuggestionItem {
        id: id.to_owned(),
        display_text: id.to_owned(),
        completion: completion.to_owned(),
        description: None,
        icon: None,
        source: "test".to_owned(),
        score,
        arg_hint: arg_hint.map(str::to_owned),
    }
}

fn ids(items: &[SuggestionItem]) -> Vec<&str> {
    items.iter().map(|item| item.id.as_str()).collect()
}

struct StubProvider {
    trigger: &'static str,
    items: Vec<SuggestionItem>,
    calls: Rc<RefCell<Vec<String>>>,
}

impl StubProvider {
    fn new(
        trigger: &'static str,
        items: Vec<SuggestionItem>,
        calls: Rc<RefCell<Vec<String>>>,
    ) -> Self {
        Self {
            trigger,
            items,
            calls,
        }
    }
}

impl SuggestionProvider for StubProvider {
    fn trigger(&self) -> &str {
        self.trigger
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        self.calls.borrow_mut().push(token.text.clone());
        self.items.clone()
    }
}
