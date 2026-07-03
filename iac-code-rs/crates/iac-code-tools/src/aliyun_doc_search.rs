use iac_code_protocol::json::{self, JsonValue};

use std::path::PathBuf;

use iac_code_config::cloud_credentials::AliyunCredential;

use crate::{
    AliyunApiTool, RosStackInstancesTool, RosStackTool, Tool, ToolContext, ToolRegistry, ToolResult,
};

mod input;
mod response;
mod schema;

use input::{build_search_params, string_field};
use response::format_search_response;
use schema::input_schema;

const SEARCH_URL: &str = "https://help.aliyun.com/help/json/search.json";
const PAGE_SIZE: usize = 10;

const ALIYUN_TOOL_NAMES: &[&str] = &[
    "aliyun_api",
    "aliyun_doc_search",
    "ros_stack",
    "ros_stack_instances",
];

#[derive(Clone, Debug)]
pub struct AliyunDocSearchTool {
    search_url: String,
}

impl AliyunDocSearchTool {
    pub fn new() -> Self {
        Self {
            search_url: SEARCH_URL.into(),
        }
    }

    pub fn with_search_url(mut self, search_url: impl Into<String>) -> Self {
        self.search_url = search_url.into();
        self
    }
}

impl Default for AliyunDocSearchTool {
    fn default() -> Self {
        Self::new()
    }
}

impl Tool for AliyunDocSearchTool {
    fn name(&self) -> &str {
        "aliyun_doc_search"
    }

    fn description(&self) -> &str {
        "Search Alibaba Cloud documentation. Returns document titles, summaries and links. Use category_id=28850 to limit results to ROS product docs."
    }

    fn input_schema(&self) -> JsonValue {
        input_schema()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        match string_field(input, "keywords") {
            Some(_) => Ok(()),
            None => Err("missing required field 'keywords'".into()),
        }
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let keywords = string_field(input, "keywords").unwrap_or_default().trim();
        if keywords.is_empty() {
            return ToolResult::error("keywords cannot be empty.");
        }

        let params = build_search_params(input, keywords, PAGE_SIZE);

        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build();
        let Ok(client) = client else {
            return ToolResult::error("Failed to search docs: failed to build HTTP client");
        };
        let response = match client.get(&self.search_url).query(&params).send() {
            Ok(response) => response,
            Err(error) => return ToolResult::error(format!("Failed to search docs: {error}")),
        };
        let status = response.status();
        if !status.is_success() {
            return ToolResult::error(format!(
                "HTTP error {} when searching docs.",
                status.as_u16()
            ));
        }
        let body = match response.text() {
            Ok(body) => body,
            Err(error) => return ToolResult::error(format!("Failed to search docs: {error}")),
        };
        let data = match json::parse(&body) {
            Ok(data) => data,
            Err(_) => return ToolResult::error("Failed to parse search response as JSON."),
        };
        if bool_field(&data, "success") != Some(true) {
            return ToolResult::error("Search API returned failure.");
        }

        ToolResult::success(format_search_response(&data, keywords))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "DocSearch".into()
    }
}

pub fn register_cloud_tools(
    registry: &mut ToolRegistry,
    aliyun_credential: Option<AliyunCredential>,
) {
    register_cloud_tools_with_cloud_credentials_path(registry, aliyun_credential, None);
}

pub fn register_cloud_tools_with_cloud_credentials_path(
    registry: &mut ToolRegistry,
    aliyun_credential: Option<AliyunCredential>,
    cloud_credentials_path: Option<PathBuf>,
) {
    for tool_name in ALIYUN_TOOL_NAMES {
        registry.unregister(tool_name);
    }
    if let Some(credential) = aliyun_credential {
        let mut api_tool = AliyunApiTool::new(Some(credential.clone()));
        let mut ros_stack_tool = RosStackTool::new(Some(credential.clone()));
        let mut ros_stack_instances_tool = RosStackInstancesTool::new(Some(credential.clone()));
        if let Some(path) = cloud_credentials_path {
            api_tool = api_tool.with_cloud_credentials_path(path.clone());
            ros_stack_tool = ros_stack_tool.with_cloud_credentials_path(path.clone());
            ros_stack_instances_tool = ros_stack_instances_tool.with_cloud_credentials_path(path);
        }
        registry.register(Box::new(api_tool));
        registry.register(Box::new(AliyunDocSearchTool::new()));
        registry.register(Box::new(ros_stack_tool));
        registry.register(Box::new(ros_stack_instances_tool));
    }
}

fn bool_field(input: &JsonValue, field: &str) -> Option<bool> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}
