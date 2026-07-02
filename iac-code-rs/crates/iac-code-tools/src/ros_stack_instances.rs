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
use status::{progress_percentage, StackInstanceStatus, TERMINAL_STATUSES};

#[derive(Clone, Debug)]
pub struct RosStackInstancesTool {
    api: AliyunApiTool,
    poll_interval: Duration,
    timeout: Duration,
}

impl RosStackInstancesTool {
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

impl Tool for RosStackInstancesTool {
    fn name(&self) -> &str {
        "ros_stack_instances"
    }

    fn description(&self) -> &str {
        "Manage Alibaba Cloud ROS (Resource Orchestration Service) StackGroup instances lifecycle. Supports creating, updating, and deleting stack instances with real-time progress polling via operation ID."
    }

    fn input_schema(&self) -> JsonValue {
        schema::input_schema()
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let action = input::string_field(input, "action").unwrap_or_default();
        if !SUPPORTED_ACTIONS.contains(&action) {
            return ToolResult::error(format!(
                "Invalid action '{action}'. Supported actions: {SUPPORTED_ACTIONS:?}"
            ));
        }

        let region = input::string_field(input, "region_id").unwrap_or_default();
        let params = input::params_with_region(input, region);
        let stack_group_name = params.get("StackGroupName").cloned().unwrap_or_default();

        let operation_id = match self.start_action(action, params, region) {
            Ok(operation_id) => operation_id,
            Err(error) => return ToolResult::error(format!("[{action}] {}", clean_error(&error))),
        };

        let started = Instant::now();
        loop {
            if started.elapsed() > self.timeout {
                return ToolResult::error(format!(
                    "[{action}] timed out waiting for stack instances operation"
                ));
            }
            std::thread::sleep(self.poll_interval);

            let status = match self.get_operation_status(&operation_id, region) {
                Ok(status) => status,
                Err(error) => {
                    return ToolResult::error(format!(
                        "[GetStackGroupOperation] {}",
                        clean_error(&error)
                    ));
                }
            };
            let instances = match self.get_instances(&stack_group_name, region) {
                Ok(instances) => instances,
                Err(error) => {
                    return ToolResult::error(format!(
                        "[ListStackInstances] {}",
                        clean_error(&error)
                    ));
                }
            };
            let progress_percentage = progress_percentage(&instances);
            let elapsed_seconds = started.elapsed().as_secs();

            if TERMINAL_STATUSES.contains(&status.as_str()) {
                return response::final_result(
                    stack_group_name,
                    operation_id,
                    status,
                    progress_percentage,
                    &instances,
                    elapsed_seconds,
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
        "CloudStackInstances".into()
    }
}

impl RosStackInstancesTool {
    fn start_action(
        &self,
        action: &str,
        params: BTreeMap<String, String>,
        region: &str,
    ) -> Result<String, String> {
        let body = self
            .api
            .execute_rpc("ros", action, "2019-09-10", params, region)?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(status::json_string(&value, "OperationId").unwrap_or_default())
    }

    fn get_operation_status(&self, operation_id: &str, region: &str) -> Result<String, String> {
        let body = self.api.execute_rpc(
            "ros",
            "GetStackGroupOperation",
            "2019-09-10",
            BTreeMap::from([("OperationId".into(), operation_id.into())]),
            region,
        )?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(status::json_string(&value, "Status").unwrap_or_else(|| "RUNNING".into()))
    }

    fn get_instances(
        &self,
        stack_group_name: &str,
        region: &str,
    ) -> Result<Vec<StackInstanceStatus>, String> {
        let body = self.api.execute_rpc(
            "ros",
            "ListStackInstances",
            "2019-09-10",
            BTreeMap::from([("StackGroupName".into(), stack_group_name.into())]),
            region,
        )?;
        let value =
            serde_json::from_str::<serde_json::Value>(&body).map_err(|error| error.to_string())?;
        Ok(value
            .get("StackInstances")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .map(StackInstanceStatus::from_value)
            .collect())
    }
}
