use std::io::{BufRead, BufReader, Read};

use iac_code_protocol::{StreamEvent, TombstoneEvent};

use super::errors::StreamChatError;
use super::response::{parse_non_streaming_response, stream_events_from_response};
use super::sse::OpenAiSseEventParser;

pub(super) fn stream_response_events_with_sink(
    response: reqwest::blocking::Response,
    sink: &mut dyn FnMut(&StreamEvent),
) -> Result<Vec<StreamEvent>, StreamChatError> {
    let mut reader = BufReader::new(response);
    let mut line = String::new();
    let mut data_lines = Vec::<String>::new();
    let mut parser = OpenAiSseEventParser::default();

    loop {
        line.clear();
        let bytes = reader
            .read_line(&mut line)
            .map_err(|error| StreamChatError::new(error.to_string(), parser.events.clone()))?;
        if bytes == 0 {
            break;
        }
        if parser.is_empty() && data_lines.is_empty() {
            let trimmed = line.trim_start();
            if trimmed.starts_with('{') {
                let mut text = line.clone();
                reader
                    .read_to_string(&mut text)
                    .map_err(|error| StreamChatError::new(error.to_string(), Vec::new()))?;
                let events = parse_non_streaming_response(&text)
                    .map(stream_events_from_response)
                    .map_err(StreamChatError::without_partial)?;
                for event in &events {
                    sink(event);
                }
                return Ok(events);
            }
        }

        let trimmed_end = line.trim_end_matches(['\r', '\n']);
        if trimmed_end.is_empty() {
            if !data_lines.is_empty() {
                let data = data_lines.join("\n");
                data_lines.clear();
                if parser.push_data(&data, sink)? {
                    break;
                }
            }
            continue;
        }
        if let Some(data) = trimmed_end.strip_prefix("data:") {
            data_lines.push(data.trim_start().to_owned());
        }
    }

    if !data_lines.is_empty() {
        let data = data_lines.join("\n");
        parser.push_data(&data, sink)?;
    }

    parser.finish(sink)
}

pub(super) fn tombstone_events_for_orphaned_messages(events: &[StreamEvent]) -> Vec<StreamEvent> {
    let mut open_message_ids = Vec::<String>::new();
    for event in events {
        match event {
            StreamEvent::MessageStart(event) => open_message_ids.push(event.message_id.clone()),
            StreamEvent::MessageEnd(_) => open_message_ids.clear(),
            _ => {}
        }
    }
    open_message_ids
        .into_iter()
        .map(|message_id| StreamEvent::Tombstone(TombstoneEvent { message_id }))
        .collect()
}
