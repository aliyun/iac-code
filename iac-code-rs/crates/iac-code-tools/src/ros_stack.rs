use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json::JsonValue;

use crate::{AliyunApiTool, Tool, ToolContext, ToolResult};

mod input;
mod response;
mod schema;
mod status;

use response::clean_error;
use schema::SUPPORTED_ACTIONS;
use status::{is_action_terminal, StackStatus};

#[derive(Clone, Debug)]
pub struct RosStackTool {
    api: AliyunApiTool,
    poll_interval: Duration,
    timeout: Duration,
}

impl RosStackTool {
    pub fn new(credential: Option<AliyunCredential>) -> Self {
        Self {
            api: AliyunApiTool::new(credential),
            poll_interval: Duration::from_secs(5),
            timeout: Duration::from_secs(3600),
        }
    }

    pub fn with_endpoint_override(
        mut self,
        product: impl Into<String>,
        endpoint: impl Into<String>,
    ) -> Self {
        self.api = self.api.with_endpoint_override(product, endpoint);
        self
    }

    pub fn with_cloud_credentials_path(mut self, path: PathBuf) -> Self {
        self.api = self.api.with_cloud_credentials_path(path);
        self
    }

    pub fn with_poll_interval(mut self, poll_interval: Duration) -> Self {
        self.poll_interval = poll_interval;
        self
    }
}

impl Tool for RosStackTool {
    fn name(&self) -> &str {
        "ros_stack"
    }

    fn description(&self) -> &str {
        "Manage Alibaba Cloud ROS (Resource Orchestration Service) stack lifecycle. Supports creating, updating, continuing, and deleting stacks with real-time progress polling."
    }

    fn input_schema(&self) -> JsonValue {
        schema::input_schema()
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let action = input::string_field(input, "action").unwrap_or_default();
        if !SUPPORTED_ACTIONS.contains(&action) {
            return ToolResult::error(format!(
                "Invalid action '{action}'. Supported actions: {SUPPORTED_ACTIONS:?}"
            ));
        }

        let region = input::string_field(input, "region_id").unwrap_or_default();
        let params = input::params_with_region_and_template(input, region, context);

        let stack_id = match self.start_action(action, params.clone(), region) {
            Ok(stack_id) => stack_id,
            Err(error) => return ToolResult::error(format!("[{action}] {}", clean_error(&error))),
        };

        let started = Instant::now();
        loop {
            if started.elapsed() > self.timeout {
                return ToolResult::error(format!("[{action}] timed out waiting for stack status"));
            }
            std::thread::sleep(self.poll_interval);

            let status = match self.get_stack_status(&stack_id, region) {
                Ok(status) => status,
                Err(error) => {
                    return ToolResult::error(format!("[GetStackStatus] {}", clean_error(&error)));
                }
            };
            let resources = match self.get_stack_resources(&stack_id, region) {
                Ok(resources) => resources,
                Err(error) if is_action_terminal(action, &status.status) => {
                    let _ = error;
                    Vec::new()
                }
                Err(error) => {
                    return ToolResult::error(format!(
                        "[GetStackResources] {}",
                        clean_error(&error)
                    ));
                }
            };

            if is_action_terminal(action, &status.status) {
                return response::final_result(
                    action,
                    status,
                    resources,
                    started.elapsed().as_secs(),
                );
            }
        }
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }

    fn is_concurrency_safe(&self, _input: &JsonValue) -> bool {
        false
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "ROS Stack".into()
    }
}

impl RosStackTool {
    fn start_action(
        &self,
        action: &str,
        params: BTreeMap<String, String>,
        region: &str,
    ) -> Result<String, String> {
        let fallback_stack_id = params.get("StackId").cloned().unwrap_or_default();
        let body = self
            .api
            .execute_rpc("ros", action, "2019-09-10", params, region)?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(status::json_string(&value, "StackId").unwrap_or(fallback_stack_id))
    }

    fn get_stack_status(&self, stack_id: &str, region: &str) -> Result<StackStatus, String> {
        let body = self.api.execute_rpc(
            "ros",
            "GetStack",
            "2019-09-10",
            BTreeMap::from([("StackId".into(), stack_id.into())]),
            region,
        )?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(StackStatus {
            stack_id: status::json_string(&value, "StackId").unwrap_or_else(|| stack_id.into()),
            stack_name: status::json_string(&value, "StackName").unwrap_or_default(),
            status: status::json_string(&value, "Status").unwrap_or_default(),
            status_reason: status::json_string(&value, "StatusReason").unwrap_or_default(),
            progress_percentage: status::json_f64(&value, "ProgressPercentage").unwrap_or(0.0),
        })
    }

    fn get_stack_resources(
        &self,
        stack_id: &str,
        region: &str,
    ) -> Result<Vec<serde_json::Value>, String> {
        let body = self.api.execute_rpc(
            "ros",
            "ListStackResources",
            "2019-09-10",
            BTreeMap::from([("StackId".into(), stack_id.into())]),
            region,
        )?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(value
            .get("Resources")
            .or_else(|| value.get("StackResources"))
            .and_then(serde_json::Value::as_array)
            .cloned()
            .unwrap_or_default())
    }
}
