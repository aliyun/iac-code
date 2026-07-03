use super::{A2ATaskStore, ListTasksRequest, ListTasksResponse, SdkTask, TaskStoreError};

const DEFAULT_LIST_TASKS_PAGE_SIZE: usize = 100;

impl A2ATaskStore {
    pub fn list_sdk_tasks(
        &self,
        params: ListTasksRequest,
        owner: &str,
    ) -> Result<ListTasksResponse, TaskStoreError> {
        let owner_tasks = self.sdk_tasks.get(owner);
        let mut tasks = if let Some(context_id) = &params.context_id {
            let task_ids = self
                .sdk_tasks_by_context
                .get(owner)
                .and_then(|contexts| contexts.get(context_id));
            task_ids
                .into_iter()
                .flat_map(|ids| ids.iter())
                .filter_map(|task_id| owner_tasks.and_then(|tasks| tasks.get(task_id)))
                .cloned()
                .collect::<Vec<_>>()
        } else {
            owner_tasks
                .into_iter()
                .flat_map(|tasks| tasks.values())
                .cloned()
                .collect::<Vec<_>>()
        };

        if let Some(status) = &params.status {
            tasks.retain(|task| &task.status == status);
        }

        tasks.sort_by(|left, right| task_sort_key(right).cmp(&task_sort_key(left)));

        let total_size = tasks.len();
        let start_idx = if let Some(page_token) = &params.page_token {
            let start_task_id = decode_page_token(page_token).ok_or_else(|| {
                TaskStoreError::InvalidParams(format!("Invalid page token: {page_token}"))
            })?;
            tasks
                .iter()
                .position(|task| task.id == start_task_id)
                .ok_or_else(|| {
                    TaskStoreError::InvalidParams(format!("Invalid page token: {page_token}"))
                })?
        } else {
            0
        };

        let page_size = params.page_size.unwrap_or(DEFAULT_LIST_TASKS_PAGE_SIZE);
        let end_idx = (start_idx + page_size).min(total_size);
        let next_page_token = if end_idx < total_size {
            encode_page_token(&tasks[end_idx].id)
        } else {
            String::new()
        };
        let page = tasks[start_idx..end_idx]
            .iter()
            .map(|task| project_task(task, params.include_artifacts))
            .collect();

        Ok(ListTasksResponse {
            tasks: page,
            next_page_token,
            page_size,
            total_size,
        })
    }
}

fn task_sort_key(task: &SdkTask) -> (bool, i64, &str) {
    (
        task.status_timestamp.is_some(),
        task.status_timestamp.unwrap_or_default(),
        task.id.as_str(),
    )
}

fn project_task(task: &SdkTask, include_artifacts: bool) -> SdkTask {
    let mut projected = task.clone();
    if !include_artifacts {
        projected.artifacts.clear();
    }
    projected
}

fn encode_page_token(task_id: &str) -> String {
    encode_base64(task_id.as_bytes())
}

fn decode_page_token(page_token: &str) -> Option<String> {
    let bytes = decode_base64(page_token)?;
    String::from_utf8(bytes).ok()
}

fn encode_base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::new();
    for chunk in bytes.chunks(3) {
        let first = chunk[0];
        let second = *chunk.get(1).unwrap_or(&0);
        let third = *chunk.get(2).unwrap_or(&0);
        let combined = ((first as u32) << 16) | ((second as u32) << 8) | third as u32;

        output.push(ALPHABET[((combined >> 18) & 0x3f) as usize] as char);
        output.push(ALPHABET[((combined >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            output.push(ALPHABET[((combined >> 6) & 0x3f) as usize] as char);
        } else {
            output.push('=');
        }
        if chunk.len() > 2 {
            output.push(ALPHABET[(combined & 0x3f) as usize] as char);
        } else {
            output.push('=');
        }
    }
    output
}

fn decode_base64(input: &str) -> Option<Vec<u8>> {
    if !input.len().is_multiple_of(4) {
        return None;
    }
    let mut output = Vec::new();
    for chunk in input.as_bytes().chunks(4) {
        let mut values = [0u8; 4];
        let mut padding = 0;
        for (index, byte) in chunk.iter().enumerate() {
            if *byte == b'=' {
                values[index] = 0;
                padding += 1;
            } else {
                values[index] = decode_base64_byte(*byte)?;
            }
        }
        let combined = ((values[0] as u32) << 18)
            | ((values[1] as u32) << 12)
            | ((values[2] as u32) << 6)
            | values[3] as u32;
        output.push(((combined >> 16) & 0xff) as u8);
        if padding < 2 {
            output.push(((combined >> 8) & 0xff) as u8);
        }
        if padding < 1 {
            output.push((combined & 0xff) as u8);
        }
    }
    Some(output)
}

fn decode_base64_byte(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}
