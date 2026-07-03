use iac_code_protocol::json::{self, JsonValue};

use crate::{Tool, ToolContext, ToolResult};

mod html;
mod input;
mod response;

use input::{missing_host, number_field, string_field};
use response::{decode_response_body, finish_content, read_limited};

const DEFAULT_MAX_LENGTH: usize = 50_000;
const MAX_DOWNLOAD_BYTES: usize = 10 * 1024 * 1024;

pub struct WebFetchTool;

impl WebFetchTool {
    pub fn new() -> Self {
        Self
    }
}

impl Default for WebFetchTool {
    fn default() -> Self {
        Self::new()
    }
}

impl Tool for WebFetchTool {
    fn name(&self) -> &str {
        "web_fetch"
    }

    fn description(&self) -> &str {
        "Fetch the content of a web page. Supports HTTP and HTTPS URLs. For HTML pages, the content is extracted as plain text (scripts and styles removed). Returns the page content truncated to max_length characters."
    }

    fn input_schema(&self) -> JsonValue {
        json::object([
            ("type", json::string("object")),
            (
                "properties",
                json::object([
                    (
                        "url",
                        json::object([
                            ("type", json::string("string")),
                            (
                                "description",
                                json::string(
                                    "The URL of the web page to fetch. Must include scheme (http:// or https://).",
                                ),
                            ),
                        ]),
                    ),
                    (
                        "max_length",
                        json::object([
                            ("type", json::string("integer")),
                            (
                                "description",
                                json::string(
                                    "Maximum number of characters to return. Defaults to 50000.",
                                ),
                            ),
                        ]),
                    ),
                ]),
            ),
            ("required", json::array([json::string("url")])),
        ])
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        match string_field(input, "url") {
            Some(_) => Ok(()),
            None => Err("missing required field 'url'".into()),
        }
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let url = string_field(input, "url").unwrap_or_default().trim();
        if url.is_empty() {
            return ToolResult::error("URL cannot be empty.");
        }
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            return ToolResult::error(format!(
                "Invalid URL: missing scheme (e.g. http:// or https://). Got: {url}"
            ));
        }
        if missing_host(url) {
            return ToolResult::error(format!("Invalid URL: missing host/netloc. Got: {url}"));
        }

        let max_length = number_field(input, "max_length").unwrap_or(DEFAULT_MAX_LENGTH as i64);
        let client = reqwest::blocking::Client::builder()
            .user_agent(
                "Mozilla/5.0 (compatible; iac-code/1.0; +https://github.com/ros-group/iac-code)",
            )
            .timeout(std::time::Duration::from_secs(30))
            .build();
        let Ok(client) = client else {
            return ToolResult::error("Unexpected error fetching URL: failed to build HTTP client");
        };

        let mut response = match client.get(url).send() {
            Ok(response) => response,
            Err(error) => {
                return ToolResult::error(format!("Failed to fetch {url}: {error}"));
            }
        };
        let status = response.status();
        if !status.is_success() {
            return ToolResult::error(format!("HTTP error {}: {url}", status.as_u16()));
        }
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_ascii_lowercase();
        let content_length = response.content_length();
        let (downloaded, download_truncated) =
            match read_limited(&mut response, content_length, MAX_DOWNLOAD_BYTES) {
                Ok(value) => value,
                Err(error) => {
                    return ToolResult::error(format!("Failed to fetch {url}: {error}"));
                }
            };
        let text = decode_response_body(&downloaded, &content_type);

        ToolResult::success(finish_content(
            text,
            &content_type,
            max_length,
            download_truncated,
        ))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Fetch".into()
    }
}
