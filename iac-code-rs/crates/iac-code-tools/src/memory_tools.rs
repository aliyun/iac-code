mod manager;
mod model;
mod parse;
mod private_fs;
mod tools;
mod validate;

pub use manager::MemoryManager;
pub use model::Memory;
pub use tools::{register_memory_tools, ReadMemoryTool, WriteMemoryTool};

const INDEX_FILE: &str = "MEMORY.md";
const MAX_INDEX_LINES: usize = 200;
const MEMORY_TYPES: &[&str] = &["feedback", "project", "reference", "user"];
