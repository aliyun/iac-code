use pulldown_cmark::CodeBlockKind;
use pulldown_cmark::Event;
use pulldown_cmark::Options;
use pulldown_cmark::Parser;
use pulldown_cmark::Tag;
use pulldown_cmark::TagEnd;

mod code_block;
mod fences;
mod inline;
mod list;
mod style;
mod table;

use code_block::render_code_block_lines;
use fences::unwrap_markdown_fences;
use inline::InlineState;
use list::{list_item_prefix, ListState};
use style::{CYAN, DIM, RESET};
use table::{render_table, TableState};

pub fn render_markdown_ansi(input: &str, width: Option<usize>) -> String {
    let normalized = unwrap_markdown_fences(input);
    MarkdownEventRenderer::new(width).render(&normalized)
}

struct MarkdownEventRenderer {
    output: Vec<String>,
    current: String,
    width: Option<usize>,
    inline: InlineState,
    lists: Vec<ListState>,
    blockquote_depth: usize,
    code_block: Option<String>,
    table: Option<TableState>,
    needs_blank_before_block: bool,
}

impl MarkdownEventRenderer {
    fn new(width: Option<usize>) -> Self {
        Self {
            output: Vec::new(),
            current: String::new(),
            width,
            inline: InlineState::default(),
            lists: Vec::new(),
            blockquote_depth: 0,
            code_block: None,
            table: None,
            needs_blank_before_block: false,
        }
    }

    fn render(mut self, input: &str) -> String {
        let mut options = Options::empty();
        options.insert(Options::ENABLE_STRIKETHROUGH);
        options.insert(Options::ENABLE_TABLES);

        for event in Parser::new_ext(input, options) {
            self.handle_event(event);
        }
        self.flush_current_line();
        self.trim_trailing_blank_lines();
        self.output.join("\n")
    }

    fn handle_event(&mut self, event: Event<'_>) {
        if self.table.is_some() {
            self.handle_table_event(event);
            return;
        }

        if self.code_block.is_some() {
            self.handle_code_block_event(event);
            return;
        }

        match event {
            Event::Start(tag) => self.start_tag(tag),
            Event::End(tag) => self.end_tag(tag),
            Event::Text(text) => self.text(&text),
            Event::Code(code) => self.inline_code(&code),
            Event::SoftBreak | Event::HardBreak => self.flush_current_line(),
            Event::Rule => {
                self.push_blank_if_needed();
                self.output.push(format!("{DIM}———{RESET}"));
                self.needs_blank_before_block = true;
            }
            Event::Html(html) | Event::InlineHtml(html) => self.text(&html),
            Event::FootnoteReference(_) | Event::TaskListMarker(_) => {}
        }
    }

    fn start_tag(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => {
                if self.needs_blank_before_block && self.lists.is_empty() {
                    self.push_blank_if_needed();
                }
                self.needs_blank_before_block = false;
            }
            Tag::Heading { .. } => {
                self.push_blank_if_needed();
                self.inline.enter_heading();
                self.needs_blank_before_block = false;
            }
            Tag::BlockQuote => {
                self.push_blank_if_needed();
                self.blockquote_depth += 1;
                self.needs_blank_before_block = false;
            }
            Tag::CodeBlock(kind) => {
                self.push_blank_if_needed();
                let _language = match kind {
                    CodeBlockKind::Fenced(language) => Some(language),
                    CodeBlockKind::Indented => None,
                };
                self.code_block = Some(String::new());
                self.needs_blank_before_block = false;
            }
            Tag::List(start) => {
                if self.needs_blank_before_block && self.lists.is_empty() {
                    self.push_blank_if_needed();
                }
                self.lists.push(ListState::new(start));
                self.needs_blank_before_block = false;
            }
            Tag::Item => self.start_list_item(),
            Tag::Emphasis => self.inline.enter_emphasis(),
            Tag::Strong => self.inline.enter_strong(),
            Tag::Strikethrough => self.inline.enter_strikethrough(),
            Tag::Link { .. } => self.inline.enter_link(),
            Tag::Table(_) => {
                self.push_blank_if_needed();
                self.table = Some(TableState::new());
            }
            Tag::TableHead
            | Tag::TableRow
            | Tag::TableCell
            | Tag::HtmlBlock
            | Tag::FootnoteDefinition(_)
            | Tag::Image { .. }
            | Tag::MetadataBlock(_) => {}
        }
    }

    fn end_tag(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph => {
                self.flush_current_line();
                self.needs_blank_before_block = true;
            }
            TagEnd::Heading(_) => {
                self.flush_current_line();
                self.inline.exit_heading();
                self.needs_blank_before_block = true;
            }
            TagEnd::BlockQuote => {
                self.flush_current_line();
                self.blockquote_depth = self.blockquote_depth.saturating_sub(1);
                self.needs_blank_before_block = true;
            }
            TagEnd::CodeBlock => {}
            TagEnd::List(_) => {
                self.lists.pop();
                self.needs_blank_before_block = true;
            }
            TagEnd::Item => {
                self.flush_current_line();
            }
            TagEnd::Emphasis => self.inline.exit_emphasis(),
            TagEnd::Strong => self.inline.exit_strong(),
            TagEnd::Strikethrough => self.inline.exit_strikethrough(),
            TagEnd::Link => self.inline.exit_link(),
            TagEnd::Table
            | TagEnd::TableHead
            | TagEnd::TableRow
            | TagEnd::TableCell
            | TagEnd::HtmlBlock
            | TagEnd::FootnoteDefinition
            | TagEnd::Image
            | TagEnd::MetadataBlock(_) => {}
        }
    }

    fn handle_code_block_event(&mut self, event: Event<'_>) {
        match event {
            Event::End(TagEnd::CodeBlock) => {
                let code = self.code_block.take().unwrap_or_default();
                self.output.extend(render_code_block_lines(&code));
                self.needs_blank_before_block = true;
            }
            Event::Text(text) | Event::Code(text) | Event::Html(text) | Event::InlineHtml(text) => {
                if let Some(code) = &mut self.code_block {
                    code.push_str(&text);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some(code) = &mut self.code_block {
                    code.push('\n');
                }
            }
            Event::Start(_)
            | Event::End(_)
            | Event::Rule
            | Event::FootnoteReference(_)
            | Event::TaskListMarker(_) => {}
        }
    }

    fn handle_table_event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(Tag::TableHead) => {
                if let Some(table) = &mut self.table {
                    table.in_header = true;
                    table.start_row();
                }
            }
            Event::End(TagEnd::TableHead) => {
                if let Some(table) = &mut self.table {
                    table.end_row();
                    table.in_header = false;
                }
            }
            Event::Start(Tag::TableRow) => {
                if let Some(table) = &mut self.table {
                    table.start_row();
                }
            }
            Event::End(TagEnd::TableRow) => {
                if let Some(table) = &mut self.table {
                    table.end_row();
                }
            }
            Event::Start(Tag::TableCell) => {
                if let Some(table) = &mut self.table {
                    table.start_cell();
                }
            }
            Event::End(TagEnd::TableCell) => {
                if let Some(table) = &mut self.table {
                    table.end_cell();
                }
            }
            Event::Text(text) | Event::Html(text) | Event::InlineHtml(text) => {
                let rendered = self.styled_text(&text);
                if let Some(table) = &mut self.table {
                    table.push_text(&rendered);
                }
            }
            Event::Code(code) => {
                let rendered = format!("{CYAN}{code}{RESET}");
                if let Some(table) = &mut self.table {
                    table.push_text(&rendered);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some(table) = &mut self.table {
                    table.hard_break();
                }
            }
            Event::Start(Tag::Emphasis) => self.inline.enter_emphasis(),
            Event::End(TagEnd::Emphasis) => self.inline.exit_emphasis(),
            Event::Start(Tag::Strong) => self.inline.enter_strong(),
            Event::End(TagEnd::Strong) => self.inline.exit_strong(),
            Event::Start(Tag::Strikethrough) => self.inline.enter_strikethrough(),
            Event::End(TagEnd::Strikethrough) => self.inline.exit_strikethrough(),
            Event::Start(Tag::Link { .. }) => self.inline.enter_link(),
            Event::End(TagEnd::Link) => self.inline.exit_link(),
            Event::End(TagEnd::Table) => {
                let table = self
                    .table
                    .take()
                    .expect("table exists while handling table");
                let rendered = render_table(&table.into_table(), self.width);
                if !rendered.is_empty() {
                    self.output.push(rendered);
                }
                self.needs_blank_before_block = true;
            }
            Event::Start(_)
            | Event::End(_)
            | Event::Rule
            | Event::FootnoteReference(_)
            | Event::TaskListMarker(_) => {}
        }
    }

    fn start_list_item(&mut self) {
        self.flush_current_line();
        if self.blockquote_depth > 0 {
            self.push_blockquote_prefix();
        }

        self.current.push_str(&list_item_prefix(&mut self.lists));
    }

    fn text(&mut self, text: &str) {
        for (index, line) in text.lines().enumerate() {
            if index > 0 {
                self.flush_current_line();
            }
            self.ensure_line_prefix();
            self.current.push_str(&self.styled_text(line));
        }
    }

    fn inline_code(&mut self, code: &str) {
        self.ensure_line_prefix();
        self.current.push_str(CYAN);
        self.current.push_str(code);
        self.current.push_str(RESET);
    }

    fn styled_text(&self, text: &str) -> String {
        self.inline.styled_text(text)
    }

    fn ensure_line_prefix(&mut self) {
        if self.current.is_empty() && self.blockquote_depth > 0 {
            self.push_blockquote_prefix();
        }
    }

    fn push_blockquote_prefix(&mut self) {
        self.current.push_str(DIM);
        self.current.push_str(&"> ".repeat(self.blockquote_depth));
        self.current.push_str(RESET);
    }

    fn flush_current_line(&mut self) {
        if !self.current.is_empty() {
            self.output.push(std::mem::take(&mut self.current));
        }
    }

    fn push_blank_if_needed(&mut self) {
        self.flush_current_line();
        if !self.output.is_empty() && !self.output.last().is_some_and(|line| line.is_empty()) {
            self.output.push(String::new());
        }
    }

    fn trim_trailing_blank_lines(&mut self) {
        while self.output.last().is_some_and(|line| line.is_empty()) {
            self.output.pop();
        }
    }
}

#[cfg(test)]
mod tests;
