pub const BUNDLED_SKILLS: &[&str] = &["iac_aliyun"];

pub const TERRAFORM_OFFICIAL_PROVIDERS: &[&str] = &[
    "alicloud",
    "aws",
    "azurerm",
    "google",
    "kubernetes",
    "oci",
    "tencentcloud",
    "huaweicloud",
    "volcengine",
    "vsphere",
    "helm",
    "null",
    "random",
    "time",
    "archive",
    "local",
    "external",
    "http",
    "tls",
];

pub const ROS_ALLOWED_PREFIXES: &[&str] = &["ALIYUN::", "DATASOURCE::"];

pub const KNOWN_MODELS: &[&str] = &[
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "o1",
    "o1-mini",
    "o3-mini",
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
    "qwen3-coder",
    "qwen2.5-coder",
    "qwen2.5-72b-instruct",
];

pub const CUSTOM_SKILL_PLACEHOLDER: &str = "custom";
pub const OTHER_MODEL_PLACEHOLDER: &str = "other";
pub const CUSTOM_TF_PROVIDER_PLACEHOLDER: &str = "other";
pub const CUSTOM_ROS_RESOURCE_PLACEHOLDER: &str = "Custom::Other";
pub const CUSTOM_TF_RESOURCE_PLACEHOLDER: &str = "custom_provider::other";
pub const MCP_TOOL_PLACEHOLDER: &str = "mcp_tool";
