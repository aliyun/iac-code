use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock, TextBlock};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AcpContentBlock {
    Text { text: String },
    EmbeddedTextResource { uri: String, text: String },
    ResourceLink { uri: String, name: String },
    Image { mime_type: String, data: String },
    Audio { mime_type: String, data: String },
    Unsupported { type_name: String },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MultimodalPart {
    Text { text: String },
    Image { mime_type: String, data: String },
    Audio { mime_type: String, data: String },
}

pub fn acp_blocks_to_prompt_text(blocks: &[AcpContentBlock]) -> String {
    blocks
        .iter()
        .map(|block| match block {
            AcpContentBlock::Text { text } => text.clone(),
            AcpContentBlock::EmbeddedTextResource { uri, text } => {
                format!("<resource uri={}>\n{}\n</resource>", py_repr(uri), text)
            }
            AcpContentBlock::ResourceLink { uri, name } => format!(
                "<resource_link uri={} name={} />",
                py_repr(uri),
                py_repr(name)
            ),
            AcpContentBlock::Image { mime_type, .. } => format!("[image: {mime_type}]"),
            AcpContentBlock::Audio { mime_type, .. } => format!("[audio: {mime_type}]"),
            AcpContentBlock::Unsupported { type_name } => {
                format!("[Unsupported ACP content block: {type_name}]")
            }
        })
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

pub fn acp_blocks_to_multimodal(blocks: &[AcpContentBlock]) -> Vec<MultimodalPart> {
    blocks
        .iter()
        .map(|block| match block {
            AcpContentBlock::Text { text } => MultimodalPart::Text { text: text.clone() },
            AcpContentBlock::EmbeddedTextResource { uri, text } => MultimodalPart::Text {
                text: format!("<resource uri={}>\n{}\n</resource>", py_repr(uri), text),
            },
            AcpContentBlock::ResourceLink { uri, name } => MultimodalPart::Text {
                text: format!(
                    "<resource_link uri={} name={} />",
                    py_repr(uri),
                    py_repr(name)
                ),
            },
            AcpContentBlock::Image { mime_type, data } => MultimodalPart::Image {
                mime_type: mime_type.clone(),
                data: data.clone(),
            },
            AcpContentBlock::Audio { mime_type, data } => MultimodalPart::Audio {
                mime_type: mime_type.clone(),
                data: data.clone(),
            },
            AcpContentBlock::Unsupported { type_name } => MultimodalPart::Text {
                text: format!("[Unsupported ACP content block: {type_name}]"),
            },
        })
        .collect()
}

pub fn acp_blocks_to_agent_message_content(blocks: &[AcpContentBlock]) -> AgentMessageContent {
    let parts = acp_blocks_to_multimodal(blocks);
    if !parts
        .iter()
        .any(|part| matches!(part, MultimodalPart::Image { .. }))
    {
        return AgentMessageContent::Text(acp_blocks_to_prompt_text(blocks));
    }

    AgentMessageContent::Blocks(
        parts
            .into_iter()
            .map(|part| match part {
                MultimodalPart::Text { text } => AgentContentBlock::Text(TextBlock { text }),
                MultimodalPart::Image { mime_type, data } => AgentContentBlock::Image(ImageBlock {
                    media_type: mime_type,
                    data,
                }),
                MultimodalPart::Audio { mime_type, .. } => AgentContentBlock::Text(TextBlock {
                    text: format!("[audio: {mime_type}]"),
                }),
            })
            .collect(),
    )
}

fn py_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output = String::new();
    output.push(quote);
    for ch in value.chars() {
        match ch {
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value == quote => {
                output.push('\\');
                output.push(value);
            }
            value if value.is_control() => output.push_str(&format!("\\x{:02x}", value as u32)),
            value => output.push(value),
        }
    }
    output.push(quote);
    output
}
