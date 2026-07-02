use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json::JsonValue;

use crate::{Tool, ToolContext, ToolResult};

mod auth;
mod encoding;
mod endpoint;
mod input;
mod oauth;
mod roa;
mod ros;
mod rpc;
mod schema;
mod time;

use endpoint::canonical_product;
use input::{
    clean_error_message, json_field, object_field, object_string_map, pretty_json_or_raw,
    string_field,
};
use roa::RoaRequest;
use ros::{expand_ros_parameters, inline_ros_template_url, validate_ros_template};

const VERSION_MAP: &[(&str, &str)] = &[
    ("ros", "2019-09-10"),
    ("ecs", "2014-05-26"),
    ("rds", "2014-08-15"),
    ("r-kvstore", "2015-01-01"),
    ("slb", "2014-05-15"),
    ("alb", "2024-03-27"),
    ("nlb", "2022-04-30"),
    ("vpc", "2016-04-28"),
    ("oss", "2019-05-17"),
    ("IaCService", "2021-08-06"),
];

const ALIYUN_ROA_USER_AGENT: &str = "AlibabaCloud Rust/iac-code-rs";
const ACCESS_TOKEN_SKEW_SECONDS: i64 = 60;
const STS_SKEW_SECONDS: i64 = 120;

#[derive(Clone, Debug)]
pub struct AliyunApiTool {
    credential: Option<AliyunCredential>,
    endpoint_overrides: HashMap<String, String>,
    oauth_base_url_override: Option<String>,
    cloud_credentials_path: Option<PathBuf>,
    now_epoch_seconds: Option<i64>,
}

impl AliyunApiTool {
    pub fn new(credential: Option<AliyunCredential>) -> Self {
        Self {
            credential,
            endpoint_overrides: HashMap::new(),
            oauth_base_url_override: None,
            cloud_credentials_path: None,
            now_epoch_seconds: None,
        }
    }

    pub fn with_endpoint_override(
        mut self,
        product: impl Into<String>,
        endpoint: impl Into<String>,
    ) -> Self {
        self.endpoint_overrides
            .insert(product.into().to_ascii_lowercase(), endpoint.into());
        self
    }

    pub fn with_oauth_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.oauth_base_url_override = Some(base_url.into());
        self
    }

    pub fn with_cloud_credentials_path(mut self, path: PathBuf) -> Self {
        self.cloud_credentials_path = Some(path);
        self
    }

    pub fn with_now_epoch_seconds(mut self, now: i64) -> Self {
        self.now_epoch_seconds = Some(now);
        self
    }

    pub fn execute_rpc(
        &self,
        product: &str,
        action: &str,
        version: &str,
        mut params: BTreeMap<String, String>,
        region: &str,
    ) -> Result<String, String> {
        let credential = self.credential_for_call()?;
        if !region.is_empty() {
            params
                .entry("RegionId".into())
                .or_insert_with(|| region.to_owned());
        }
        let endpoint = self.endpoint_url(product, region);
        self.call_rpc(&endpoint, product, action, version, params, &credential)
    }

    fn execute_roa(&self, product: &str, request: RoaRequest) -> Result<String, String> {
        let credential = self.credential_for_call()?;
        let endpoint = self.endpoint_url(product, "");
        self.call_roa(&endpoint, request, &credential)
    }
}

impl Tool for AliyunApiTool {
    fn name(&self) -> &str {
        "aliyun_api"
    }

    fn description(&self) -> &str {
        "Call any Alibaba Cloud product API through the common OpenAPI SDK. Supports ECS, RDS, Redis, SLB, ALB, VPC, OSS, ROS, and more."
    }

    fn input_schema(&self) -> JsonValue {
        schema::input_schema()
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let product_raw = string_field(input, "product").unwrap_or_default();
        let product = canonical_product(product_raw);
        let action = string_field(input, "action").unwrap_or_default();
        let region = string_field(input, "region_id")
            .filter(|value| !value.is_empty())
            .or_else(|| {
                self.credential
                    .as_ref()
                    .map(|credential| credential.region_id.as_str())
            })
            .unwrap_or_default();
        let mut raw_params = object_field(input, "params").cloned().unwrap_or_default();
        if product == "ros" {
            expand_ros_parameters(action, &mut raw_params);
        }
        let mut params = object_string_map(Some(&raw_params));
        if product == "ros" {
            inline_ros_template_url(&mut params, context);
        }
        if product == "ros" {
            if let Some(result) = validate_ros_template(action, &params) {
                return result;
            }
        }

        let version = match self.resolve_version(&product, string_field(input, "version")) {
            Ok(version) => version,
            Err(error) => return ToolResult::error(error),
        };
        if self.credential.is_none() {
            return ToolResult::error(
                "Alibaba Cloud credentials not configured. Run 'iac-code auth' and select 'Cloud Provider' to configure.",
            );
        }

        let style = string_field(input, "style").unwrap_or("RPC");
        let result = if style.eq_ignore_ascii_case("ROA") {
            let method = string_field(input, "method").unwrap_or("POST");
            let pathname = string_field(input, "pathname").unwrap_or("/");
            let body = json_field(input, "body").cloned();
            self.execute_roa(
                &product,
                RoaRequest {
                    action: action.to_owned(),
                    version: version.clone(),
                    params,
                    method: method.to_owned(),
                    pathname: pathname.to_owned(),
                    body,
                },
            )
        } else {
            self.execute_rpc(&product, action, &version, params, region)
        };
        match result {
            Ok(body) => ToolResult::success(pretty_json_or_raw(&body)),
            Err(error) => ToolResult::error(clean_error_message(&error)),
        }
    }

    fn is_read_only(&self, input: &JsonValue) -> bool {
        let product = string_field(input, "product").unwrap_or_default();
        let action = string_field(input, "action").unwrap_or_default();
        if product == "ros" && action == "PreviewStack" {
            return true;
        }
        action.starts_with("Get")
            || action.starts_with("List")
            || action.starts_with("Describe")
            || action.starts_with("Query")
            || action.starts_with("Validate")
    }

    fn is_concurrency_safe(&self, input: &JsonValue) -> bool {
        self.is_read_only(input)
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Aliyun API".into()
    }
}
