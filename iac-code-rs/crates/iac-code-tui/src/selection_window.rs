#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SelectionWindow {
    focused_index: usize,
    visible_from: usize,
    visible_count: usize,
}

impl SelectionWindow {
    pub(crate) fn new(visible_count: usize) -> Self {
        Self {
            focused_index: 0,
            visible_from: 0,
            visible_count,
        }
    }

    pub(crate) fn reset(&mut self) {
        self.focused_index = 0;
        self.visible_from = 0;
    }

    pub(crate) fn reset_with_focus(&mut self, item_count: usize, focused_index: usize) {
        self.focused_index = if item_count == 0 {
            0
        } else {
            focused_index.min(item_count - 1)
        };
        self.visible_from = 0;
    }

    pub(crate) fn focused_index(&self) -> usize {
        self.focused_index
    }

    pub(crate) fn visible_from(&self) -> usize {
        self.visible_from
    }

    pub(crate) fn visible_slice<'a, T>(&self, items: &'a [T]) -> &'a [T] {
        let start = self.visible_from.min(items.len());
        let end = (start + self.visible_count).min(items.len());
        &items[start..end]
    }

    pub(crate) fn focused<'a, T>(&self, items: &'a [T]) -> Option<&'a T> {
        items.get(self.focused_index)
    }

    pub(crate) fn move_focus(&mut self, item_count: usize, delta: isize) {
        if item_count == 0 {
            return;
        }
        self.focused_index = self
            .focused_index
            .saturating_add_signed(delta)
            .min(item_count - 1);
        self.update_scroll();
    }

    pub(crate) fn page_up(&mut self, item_count: usize) {
        self.move_focus(item_count, -(self.visible_count as isize));
    }

    pub(crate) fn page_down(&mut self, item_count: usize) {
        self.move_focus(item_count, self.visible_count as isize);
    }

    fn update_scroll(&mut self) {
        if self.focused_index < self.visible_from {
            self.visible_from = self.focused_index;
        } else if self.focused_index >= self.visible_from + self.visible_count {
            self.visible_from = self.focused_index - self.visible_count + 1;
        }
    }
}
