use super::model::Memory;

pub(super) fn parse_memory_file(text: &str) -> Memory {
    let mut memory = Memory::default();
    if let Some(rest) = text.strip_prefix("---") {
        if let Some((frontmatter, content)) = rest.split_once("---") {
            for line in frontmatter.trim().split('\n') {
                if let Some((key, value)) = line.split_once(':') {
                    match key.trim() {
                        "name" => memory.name = value.trim().to_owned(),
                        "description" => memory.description = value.trim().to_owned(),
                        "type" => memory.memory_type = value.trim().to_owned(),
                        _ => {}
                    }
                }
            }
            memory.content = content.trim().to_owned();
            return memory;
        }
    }
    memory.content = text.to_owned();
    memory
}
