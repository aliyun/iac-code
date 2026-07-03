#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Memory {
    pub name: String,
    pub description: String,
    pub memory_type: String,
    pub content: String,
}
