use crate::ansi::ansi_display_width;

use super::style::{BOLD_CYAN, CYAN, RESET};

#[derive(Debug)]
pub(super) struct TableState {
    header: Option<Vec<String>>,
    rows: Vec<Vec<String>>,
    current_row: Option<Vec<String>>,
    current_cell: Option<String>,
    pub(super) in_header: bool,
}

impl TableState {
    pub(super) fn new() -> Self {
        Self {
            header: None,
            rows: Vec::new(),
            current_row: None,
            current_cell: None,
            in_header: false,
        }
    }

    pub(super) fn push_text(&mut self, text: &str) {
        if let Some(cell) = &mut self.current_cell {
            cell.push_str(text);
        }
    }

    pub(super) fn hard_break(&mut self) {
        if let Some(cell) = &mut self.current_cell {
            cell.push(' ');
        }
    }

    pub(super) fn start_row(&mut self) {
        self.current_row = Some(Vec::new());
    }

    pub(super) fn end_row(&mut self) {
        let Some(row) = self.current_row.take() else {
            return;
        };
        if self.in_header {
            self.header = Some(row);
        } else {
            self.rows.push(row);
        }
    }

    pub(super) fn start_cell(&mut self) {
        self.current_cell = Some(String::new());
    }

    pub(super) fn end_cell(&mut self) {
        let Some(cell) = self.current_cell.take() else {
            return;
        };
        self.current_row.get_or_insert_with(Vec::new).push(cell);
    }

    pub(super) fn into_table(self) -> MarkdownTable {
        MarkdownTable {
            header: self.header.unwrap_or_default(),
            rows: self.rows,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct MarkdownTable {
    header: Vec<String>,
    rows: Vec<Vec<String>>,
}

pub(super) fn render_table(table: &MarkdownTable, width: Option<usize>) -> String {
    let column_count = table
        .rows
        .iter()
        .fold(table.header.len(), |count, row| count.max(row.len()));
    if column_count == 0 {
        return String::new();
    }

    let mut widths = vec![0usize; column_count];
    for (index, cell) in table.header.iter().enumerate() {
        widths[index] = widths[index].max(ansi_display_width(cell));
    }
    for row in &table.rows {
        for (index, cell) in row.iter().enumerate() {
            widths[index] = widths[index].max(ansi_display_width(cell));
        }
    }
    for width in &mut widths {
        *width = (*width).max(3);
    }

    let rendered_width = widths.iter().sum::<usize>() + column_count.saturating_sub(1) * 3;
    if width.is_some_and(|limit| rendered_width > limit.saturating_sub(2)) {
        return render_table_records(table);
    }

    let mut output = String::new();
    output.push_str(&render_table_row(&table.header, &widths, true));
    output.push('\n');
    for (index, width) in widths.iter().enumerate() {
        if index > 0 {
            output.push_str("   ");
        }
        output.push_str(CYAN);
        output.push_str(&"─".repeat(*width));
        output.push_str(RESET);
    }
    for row in &table.rows {
        output.push('\n');
        output.push_str(&render_table_row(row, &widths, false));
    }
    output
}

fn render_table_row(row: &[String], widths: &[usize], header: bool) -> String {
    let mut output = String::new();
    for (index, width) in widths.iter().enumerate() {
        if index > 0 {
            output.push_str("   ");
        }
        let cell = row.get(index).map(String::as_str).unwrap_or_default();
        if header {
            output.push_str(BOLD_CYAN);
        }
        output.push_str(cell);
        if header {
            output.push_str(RESET);
        }
        output.push_str(&" ".repeat(width.saturating_sub(ansi_display_width(cell))));
    }
    output
}

fn render_table_records(table: &MarkdownTable) -> String {
    let mut lines = Vec::new();
    for row in &table.rows {
        for (index, cell) in row.iter().enumerate() {
            let label = table
                .header
                .get(index)
                .filter(|label| !label.is_empty())
                .map(String::as_str)
                .unwrap_or("Column");
            lines.push(format!("{BOLD_CYAN}{label}{RESET}: {cell}"));
        }
        lines.push(String::new());
    }
    while lines.last().is_some_and(|line| line.is_empty()) {
        lines.pop();
    }
    lines.join("\n")
}
