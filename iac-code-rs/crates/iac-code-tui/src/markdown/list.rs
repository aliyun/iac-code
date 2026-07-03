#[derive(Debug)]
pub(super) struct ListState {
    next: Option<u64>,
}

impl ListState {
    pub(super) fn new(next: Option<u64>) -> Self {
        Self { next }
    }

    fn marker(&mut self) -> String {
        match self.next.as_mut() {
            Some(next) => {
                let marker = format!("{next}. ");
                *next += 1;
                marker
            }
            None => "- ".to_string(),
        }
    }
}

pub(super) fn list_item_prefix(lists: &mut [ListState]) -> String {
    let depth = lists.len().max(1);
    let mut prefix = " ".repeat((depth - 1) * 2);
    let marker = lists
        .last_mut()
        .map(ListState::marker)
        .unwrap_or_else(|| "- ".to_string());
    prefix.push_str(&marker);
    prefix
}
