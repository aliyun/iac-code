use crate::session::MemoryEntry;

pub(super) fn format_memory_summary(title: &str, mut memories: Vec<MemoryEntry>) -> Option<String> {
    if memories.is_empty() {
        return None;
    }

    memories.sort_by(|left, right| left.name.cmp(&right.name));
    let mut output = title.to_owned();
    for memory in memories {
        output.push_str(&format!("\n  - {} - {}", memory.name, memory.description));
    }
    Some(output)
}

pub(super) fn format_memory_detail(memory: MemoryEntry) -> String {
    format!(
        "[{}] {}\n\n{}",
        memory.memory_type, memory.description, memory.content
    )
}
