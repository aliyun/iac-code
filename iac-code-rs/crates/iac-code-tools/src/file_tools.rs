use crate::{BashTool, ToolRegistry, WebFetchTool};

mod common;
mod edit;
mod glob;
mod glob_match;
mod grep;
mod list;
mod read;
mod write;

pub use edit::EditFileTool;
pub use glob::GlobTool;
pub use grep::GrepTool;
pub use list::ListFilesTool;
pub use read::ReadFileTool;
pub use write::WriteFileTool;

pub fn register_file_tools(registry: &mut ToolRegistry) {
    registry.register(Box::new(ReadFileTool::new()));
    registry.register(Box::new(WriteFileTool::new()));
    registry.register(Box::new(EditFileTool::new()));
    registry.register(Box::new(BashTool::new()));
    registry.register(Box::new(ListFilesTool::new()));
    registry.register(Box::new(GlobTool::new()));
    registry.register(Box::new(GrepTool::new()));
    registry.register(Box::new(WebFetchTool::new()));
}
