pub mod gen_ai_span_kind {
    pub const ENTRY: &str = "ENTRY";
    pub const LLM: &str = "LLM";
    pub const TOOL: &str = "TOOL";
    pub const STEP: &str = "STEP";
    pub const AGENT: &str = "AGENT";
    pub const CHAIN: &str = "CHAIN";
    pub const TASK: &str = "TASK";
    pub const RETRIEVER: &str = "RETRIEVER";
    pub const EMBEDDING: &str = "EMBEDDING";
    pub const RERANKER: &str = "RERANKER";
}

pub mod gen_ai_operation_name {
    pub const ENTER: &str = "enter";
    pub const CHAT: &str = "chat";
    pub const TEXT_COMPLETION: &str = "text_completion";
    pub const GENERATE_CONTENT: &str = "generate_content";
    pub const EXECUTE_TOOL: &str = "execute_tool";
    pub const INVOKE_AGENT: &str = "invoke_agent";
    pub const CREATE_AGENT: &str = "create_agent";
    pub const REACT: &str = "react";
    pub const RETRIEVAL: &str = "retrieval";
    pub const EMBEDDINGS: &str = "embeddings";
}

pub mod gen_ai_attr {
    pub const SPAN_KIND: &str = "gen_ai.span.kind";
    pub const OPERATION_NAME: &str = "gen_ai.operation.name";
    pub const SESSION_ID: &str = "gen_ai.session.id";
    pub const USER_ID: &str = "gen_ai.user.id";
    pub const FRAMEWORK: &str = "gen_ai.framework";
    pub const PROVIDER_NAME: &str = "gen_ai.provider.name";
    pub const REQUEST_MODEL: &str = "gen_ai.request.model";
    pub const RESPONSE_MODEL: &str = "gen_ai.response.model";
    pub const RESPONSE_ID: &str = "gen_ai.response.id";
    pub const CONVERSATION_ID: &str = "gen_ai.conversation.id";
    pub const REQUEST_MAX_TOKENS: &str = "gen_ai.request.max_tokens";
    pub const REQUEST_TEMPERATURE: &str = "gen_ai.request.temperature";
    pub const REQUEST_TOP_P: &str = "gen_ai.request.top_p";
    pub const REQUEST_TOP_K: &str = "gen_ai.request.top_k";
    pub const REQUEST_FREQUENCY_PENALTY: &str = "gen_ai.request.frequency_penalty";
    pub const REQUEST_PRESENCE_PENALTY: &str = "gen_ai.request.presence_penalty";
    pub const REQUEST_STOP_SEQUENCES: &str = "gen_ai.request.stop_sequences";
    pub const REQUEST_SEED: &str = "gen_ai.request.seed";
    pub const REQUEST_CHOICE_COUNT: &str = "gen_ai.request.choice.count";
    pub const RESPONSE_FINISH_REASONS: &str = "gen_ai.response.finish_reasons";
    pub const RESPONSE_TIME_TO_FIRST_TOKEN: &str = "gen_ai.response.time_to_first_token";
    pub const USER_TIME_TO_FIRST_TOKEN: &str = "gen_ai.user.time_to_first_token";
    pub const RESPONSE_REASONING_TIME: &str = "gen_ai.response.reasoning_time";
    pub const OUTPUT_TYPE: &str = "gen_ai.output.type";
    pub const USAGE_INPUT_TOKENS: &str = "gen_ai.usage.input_tokens";
    pub const USAGE_OUTPUT_TOKENS: &str = "gen_ai.usage.output_tokens";
    pub const USAGE_TOTAL_TOKENS: &str = "gen_ai.usage.total_tokens";
    pub const USAGE_CACHE_CREATION_INPUT_TOKENS: &str = "gen_ai.usage.cache_creation.input_tokens";
    pub const USAGE_CACHE_READ_INPUT_TOKENS: &str = "gen_ai.usage.cache_read.input_tokens";
    pub const INPUT_MESSAGES: &str = "gen_ai.input.messages";
    pub const OUTPUT_MESSAGES: &str = "gen_ai.output.messages";
    pub const SYSTEM_INSTRUCTIONS: &str = "gen_ai.system_instructions";
    pub const TOOL_DEFINITIONS: &str = "gen_ai.tool.definitions";
    pub const TOOL_NAME: &str = "gen_ai.tool.name";
    pub const TOOL_TYPE: &str = "gen_ai.tool.type";
    pub const TOOL_CALL_ID: &str = "gen_ai.tool.call.id";
    pub const TOOL_DESCRIPTION: &str = "gen_ai.tool.description";
    pub const TOOL_CALL_ARGUMENTS: &str = "gen_ai.tool.call.arguments";
    pub const TOOL_CALL_RESULT: &str = "gen_ai.tool.call.result";
    pub const AGENT_NAME: &str = "gen_ai.agent.name";
    pub const AGENT_ID: &str = "gen_ai.agent.id";
    pub const AGENT_DESCRIPTION: &str = "gen_ai.agent.description";
    pub const DATA_SOURCE_ID: &str = "gen_ai.data_source.id";
    pub const REACT_FINISH_REASON: &str = "gen_ai.react.finish_reason";
    pub const REACT_ROUND: &str = "gen_ai.react.round";
}

pub mod arms_resource_attr {
    pub const SERVICE_FEATURE: &str = "acs.arms.service.feature";
    pub const CMS_WORKSPACE: &str = "acs.cms.workspace";
    pub const SERVICE_ID: &str = "acs.arms.service.id";
}

pub const ARMS_FEATURE_GENAI_APP: &str = "genai_app";
pub const FRAMEWORK_IAC_CODE: &str = "iac-code-cli";

pub mod events {
    pub const INIT: &str = "iac.init";
    pub const SESSION_STARTED: &str = "iac.session.started";
    pub const SESSION_EXITED: &str = "iac.session.exited";
    pub const SESSION_CANCELLED: &str = "iac.session.cancelled";
    pub const AUTH_CONFIGURED: &str = "iac.auth.configured";
    pub const API_REQUEST_STARTED: &str = "iac.api.request.started";
    pub const API_REQUEST_SUCCEEDED: &str = "iac.api.request.succeeded";
    pub const API_REQUEST_FAILED: &str = "iac.api.request.failed";
    pub const API_REQUEST_RETRIED: &str = "iac.api.request.retried";
    pub const MODEL_FALLBACK_TRIGGERED: &str = "iac.model.fallback.triggered";
    pub const TOOL_USE_SUCCEEDED: &str = "iac.tool.use.succeeded";
    pub const TOOL_USE_FAILED: &str = "iac.tool.use.failed";
    pub const TOOL_USE_GRANTED_IN_PROMPT: &str = "iac.tool.use.granted_in_prompt";
    pub const TOOL_USE_REJECTED_IN_PROMPT: &str = "iac.tool.use.rejected_in_prompt";
    pub const TEMPLATE_GENERATED: &str = "iac.template.generated";
    pub const TEMPLATE_VALIDATED: &str = "iac.template.validated";
    pub const DEPLOYMENT_STARTED: &str = "iac.deployment.started";
    pub const DEPLOYMENT_SUCCEEDED: &str = "iac.deployment.succeeded";
    pub const DEPLOYMENT_FAILED: &str = "iac.deployment.failed";
    pub const DEPLOYMENT_CANCELLED: &str = "iac.deployment.cancelled";
    pub const DOC_SEARCHED: &str = "iac.doc.searched";
    pub const SKILL_INVOKED: &str = "iac.skill.invoked";
    pub const SKILL_COMPLETED: &str = "iac.skill.completed";
    pub const ALIYUN_API_CALLED: &str = "iac.aliyun.api.called";
    pub const MEMORY_COMPACT_SUCCEEDED: &str = "iac.memory.compact.succeeded";
    pub const MEMORY_COMPACT_FAILED: &str = "iac.memory.compact.failed";
    pub const EXCEPTION_UNCAUGHT: &str = "iac.exception.uncaught";
    pub const EXCEPTION_UNHANDLED: &str = "iac.exception.unhandled";
    pub const QUERY_FAILED: &str = "iac.query.failed";

    pub const ALL: [&str; 29] = [
        INIT,
        SESSION_STARTED,
        SESSION_EXITED,
        SESSION_CANCELLED,
        AUTH_CONFIGURED,
        API_REQUEST_STARTED,
        API_REQUEST_SUCCEEDED,
        API_REQUEST_FAILED,
        API_REQUEST_RETRIED,
        MODEL_FALLBACK_TRIGGERED,
        TOOL_USE_SUCCEEDED,
        TOOL_USE_FAILED,
        TOOL_USE_GRANTED_IN_PROMPT,
        TOOL_USE_REJECTED_IN_PROMPT,
        TEMPLATE_GENERATED,
        TEMPLATE_VALIDATED,
        DEPLOYMENT_STARTED,
        DEPLOYMENT_SUCCEEDED,
        DEPLOYMENT_FAILED,
        DEPLOYMENT_CANCELLED,
        DOC_SEARCHED,
        SKILL_INVOKED,
        SKILL_COMPLETED,
        ALIYUN_API_CALLED,
        MEMORY_COMPACT_SUCCEEDED,
        MEMORY_COMPACT_FAILED,
        EXCEPTION_UNCAUGHT,
        EXCEPTION_UNHANDLED,
        QUERY_FAILED,
    ];
}

pub mod metrics {
    pub const SESSION_COUNT: &str = "iac.session.count";
    pub const ACTIVE_TIME_TOTAL: &str = "iac.active_time.total";
    pub const TOKEN_USAGE: &str = "iac.token.usage";
    pub const API_REQUEST_COUNT: &str = "iac.api.request.count";
    pub const API_REQUEST_DURATION: &str = "iac.api.request.duration";
    pub const TOOL_USE_COUNT: &str = "iac.tool.use.count";
    pub const TEMPLATE_GENERATED_COUNT: &str = "iac.template.generated.count";
    pub const TEMPLATE_VALIDATED_COUNT: &str = "iac.template.validated.count";
    pub const DEPLOYMENT_COUNT: &str = "iac.deployment.count";
    pub const DEPLOYMENT_DURATION: &str = "iac.deployment.duration";
    pub const RESOURCE_TYPE_OBSERVED_COUNT: &str = "iac.resource_type.observed.count";
    pub const ALIYUN_API_CALLED_COUNT: &str = "iac.aliyun.api.called.count";
    pub const ALIYUN_API_CALLED_DURATION: &str = "iac.aliyun.api.called.duration";
    pub const TERRAFORM_PROVIDER_OBSERVED_COUNT: &str = "iac.terraform.provider.observed.count";

    pub const ALL: [&str; 14] = [
        SESSION_COUNT,
        ACTIVE_TIME_TOTAL,
        TOKEN_USAGE,
        API_REQUEST_COUNT,
        API_REQUEST_DURATION,
        TOOL_USE_COUNT,
        TEMPLATE_GENERATED_COUNT,
        TEMPLATE_VALIDATED_COUNT,
        DEPLOYMENT_COUNT,
        DEPLOYMENT_DURATION,
        RESOURCE_TYPE_OBSERVED_COUNT,
        ALIYUN_API_CALLED_COUNT,
        ALIYUN_API_CALLED_DURATION,
        TERRAFORM_PROVIDER_OBSERVED_COUNT,
    ];
}

pub mod spans {
    pub const ENTRY: &str = "enter_ai_application_system";
    pub const LLM_CHAT: &str = "chat";
    pub const TOOL_EXECUTE: &str = "execute_tool";
    pub const REACT_STEP: &str = "react step";
    pub const SKILL_EXECUTE: &str = "iac.skill.execute";
    pub const TEMPLATE_VALIDATE: &str = "iac.template.validate";
}
