"""Centralized constants for all telemetry signal names."""

from __future__ import annotations

# =====================================================================
# ARMS LLM semantic conventions (gen_ai.*)
# https://help.aliyun.com/zh/arms/application-monitoring/developer-reference/llm-trace-field-definition-description
# =====================================================================


class GenAiSpanKind:
    """gen_ai.span.kind enumeration values."""

    ENTRY = "ENTRY"
    LLM = "LLM"
    TOOL = "TOOL"
    STEP = "STEP"
    AGENT = "AGENT"
    CHAIN = "CHAIN"
    TASK = "TASK"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"


class GenAiOperationName:
    """gen_ai.operation.name enumeration values."""

    ENTER = "enter"
    CHAT = "chat"
    TEXT_COMPLETION = "text_completion"
    GENERATE_CONTENT = "generate_content"
    EXECUTE_TOOL = "execute_tool"
    INVOKE_AGENT = "invoke_agent"
    CREATE_AGENT = "create_agent"
    REACT = "react"
    RETRIEVAL = "retrieval"
    EMBEDDINGS = "embeddings"


class GenAiAttr:
    """gen_ai.* span attribute key constants."""

    # --- Common (all spans) ---
    SPAN_KIND = "gen_ai.span.kind"
    OPERATION_NAME = "gen_ai.operation.name"
    SESSION_ID = "gen_ai.session.id"
    USER_ID = "gen_ai.user.id"
    FRAMEWORK = "gen_ai.framework"

    # --- Provider & Model ---
    PROVIDER_NAME = "gen_ai.provider.name"
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"
    RESPONSE_ID = "gen_ai.response.id"
    CONVERSATION_ID = "gen_ai.conversation.id"

    # --- Request parameters ---
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    REQUEST_TOP_P = "gen_ai.request.top_p"
    REQUEST_TOP_K = "gen_ai.request.top_k"
    REQUEST_FREQUENCY_PENALTY = "gen_ai.request.frequency_penalty"
    REQUEST_PRESENCE_PENALTY = "gen_ai.request.presence_penalty"
    REQUEST_STOP_SEQUENCES = "gen_ai.request.stop_sequences"
    REQUEST_SEED = "gen_ai.request.seed"
    REQUEST_CHOICE_COUNT = "gen_ai.request.choice.count"

    # --- Response ---
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
    RESPONSE_TIME_TO_FIRST_TOKEN = "gen_ai.response.time_to_first_token"
    USER_TIME_TO_FIRST_TOKEN = "gen_ai.user.time_to_first_token"
    RESPONSE_REASONING_TIME = "gen_ai.response.reasoning_time"
    OUTPUT_TYPE = "gen_ai.output.type"

    # --- Usage ---
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
    USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
    USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"

    # --- Content (debug mode only) ---
    INPUT_MESSAGES = "gen_ai.input.messages"
    OUTPUT_MESSAGES = "gen_ai.output.messages"
    SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
    TOOL_DEFINITIONS = "gen_ai.tool.definitions"

    # --- Tool ---
    TOOL_NAME = "gen_ai.tool.name"
    TOOL_TYPE = "gen_ai.tool.type"
    TOOL_CALL_ID = "gen_ai.tool.call.id"
    TOOL_DESCRIPTION = "gen_ai.tool.description"
    TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
    TOOL_CALL_RESULT = "gen_ai.tool.call.result"

    # --- Agent ---
    AGENT_NAME = "gen_ai.agent.name"
    AGENT_ID = "gen_ai.agent.id"
    AGENT_DESCRIPTION = "gen_ai.agent.description"
    DATA_SOURCE_ID = "gen_ai.data_source.id"

    # --- ReAct Step ---
    REACT_FINISH_REASON = "gen_ai.react.finish_reason"
    REACT_ROUND = "gen_ai.react.round"


class ArmsResourceAttr:
    """ARMS-specific resource attribute keys."""

    SERVICE_FEATURE = "acs.arms.service.feature"
    CMS_WORKSPACE = "acs.cms.workspace"
    SERVICE_ID = "acs.arms.service.id"


ARMS_FEATURE_GENAI_APP = "genai_app"
FRAMEWORK_IAC_CODE = "iac-code-cli"


# =====================================================================
# iac-code application signals (iac.*)
# =====================================================================


class Events:
    """All iac.* event names (OTel Logs signal)."""

    # --- Lifecycle (5) ---
    INIT = "iac.init"
    SESSION_STARTED = "iac.session.started"
    SESSION_EXITED = "iac.session.exited"
    SESSION_CANCELLED = "iac.session.cancelled"
    AUTH_CONFIGURED = "iac.auth.configured"

    # --- API / LLM (5) ---
    API_REQUEST_STARTED = "iac.api.request.started"
    API_REQUEST_SUCCEEDED = "iac.api.request.succeeded"
    API_REQUEST_FAILED = "iac.api.request.failed"
    API_REQUEST_RETRIED = "iac.api.request.retried"
    MODEL_FALLBACK_TRIGGERED = "iac.model.fallback.triggered"

    # --- Tool (6) ---
    TOOL_USE_SUCCEEDED = "iac.tool.use.succeeded"
    TOOL_USE_FAILED = "iac.tool.use.failed"
    TOOL_USE_GRANTED_IN_PROMPT = "iac.tool.use.granted_in_prompt"
    TOOL_USE_REJECTED_IN_PROMPT = "iac.tool.use.rejected_in_prompt"
    TOOL_PERMISSION_GRANTED = "iac.tool.permission.granted"
    TOOL_PERMISSION_REJECTED = "iac.tool.permission.rejected"

    # --- IaC core (9) ---
    TEMPLATE_GENERATED = "iac.template.generated"
    TEMPLATE_VALIDATED = "iac.template.validated"
    DEPLOYMENT_STARTED = "iac.deployment.started"
    DEPLOYMENT_SUCCEEDED = "iac.deployment.succeeded"
    DEPLOYMENT_FAILED = "iac.deployment.failed"
    DEPLOYMENT_CANCELLED = "iac.deployment.cancelled"
    DOC_SEARCHED = "iac.doc.searched"
    SKILL_INVOKED = "iac.skill.invoked"
    SKILL_COMPLETED = "iac.skill.completed"

    # --- Aliyun API (1) ---
    ALIYUN_API_CALLED = "iac.aliyun.api.called"

    # --- Memory (2) ---
    MEMORY_COMPACT_SUCCEEDED = "iac.memory.compact.succeeded"
    MEMORY_COMPACT_FAILED = "iac.memory.compact.failed"

    # --- Pipeline (7) ---
    PIPELINE_STARTED = "iac.pipeline.started"
    PIPELINE_COMPLETED = "iac.pipeline.completed"
    PIPELINE_RESUMED = "iac.pipeline.resumed"
    PIPELINE_USER_ABORTED = "iac.pipeline.user_aborted"
    PIPELINE_STEP_STARTED = "iac.pipeline.step.started"
    PIPELINE_STEP_COMPLETED = "iac.pipeline.step.completed"
    PIPELINE_STEP_FAILED = "iac.pipeline.step.failed"
    PIPELINE_ROLLBACK = "iac.pipeline.rollback"

    # --- Pipeline (nested / critical paths) ---
    PIPELINE_SUB_PIPELINE_STARTED = "iac.pipeline.sub_pipeline.started"
    PIPELINE_SUB_PIPELINE_COMPLETED = "iac.pipeline.sub_pipeline.completed"
    PIPELINE_SUB_PIPELINE_FAILED = "iac.pipeline.sub_pipeline.failed"
    PIPELINE_SUB_STEP_STARTED = "iac.pipeline.sub_step.started"
    PIPELINE_SUB_STEP_COMPLETED = "iac.pipeline.sub_step.completed"
    PIPELINE_SUB_STEP_FAILED = "iac.pipeline.sub_step.failed"
    PIPELINE_CANDIDATE_CANCELLED = "iac.pipeline.candidate.cancelled"
    PIPELINE_STEP_NUDGED = "iac.pipeline.step.nudged"
    PIPELINE_HARD_INTERRUPT = "iac.pipeline.hard_interrupt"
    PIPELINE_SIDECAR_FAILED = "iac.pipeline.sidecar.failed"
    PIPELINE_USER_INPUT_REQUIRED = "iac.pipeline.user_input.required"
    PIPELINE_USER_INPUT_RECEIVED = "iac.pipeline.user_input.received"
    PIPELINE_SELECTION_MADE = "iac.pipeline.selection.made"
    PIPELINE_CANDIDATES_EVALUATED = "iac.pipeline.candidates.evaluated"
    PIPELINE_FUNNEL_STEP = "iac.pipeline.funnel.step"

    # --- Crash / error (3) ---
    EXCEPTION_UNCAUGHT = "iac.exception.uncaught"
    EXCEPTION_UNHANDLED = "iac.exception.unhandled"
    QUERY_FAILED = "iac.query.failed"


class Metrics:
    """All iac.* metric names."""

    SESSION_COUNT = "iac.session.count"
    ACTIVE_TIME_TOTAL = "iac.active_time.total"
    TOKEN_USAGE = "iac.token.usage"
    API_REQUEST_COUNT = "iac.api.request.count"
    API_REQUEST_DURATION = "iac.api.request.duration"
    TOOL_USE_COUNT = "iac.tool.use.count"
    TEMPLATE_GENERATED_COUNT = "iac.template.generated.count"
    TEMPLATE_VALIDATED_COUNT = "iac.template.validated.count"
    DEPLOYMENT_COUNT = "iac.deployment.count"
    DEPLOYMENT_DURATION = "iac.deployment.duration"
    RESOURCE_TYPE_OBSERVED_COUNT = "iac.resource_type.observed.count"
    ALIYUN_API_CALLED_COUNT = "iac.aliyun.api.called.count"
    ALIYUN_API_CALLED_DURATION = "iac.aliyun.api.called.duration"
    TERRAFORM_PROVIDER_OBSERVED_COUNT = "iac.terraform.provider.observed.count"

    # Pipeline
    PIPELINE_STEP_DURATION = "iac.pipeline.step.duration"
    PIPELINE_ROLLBACK_COUNT = "iac.pipeline.rollback.count"
    PIPELINE_COMPLETION_TIME = "iac.pipeline.completion.time"
    PIPELINE_SUB_PIPELINE_DURATION = "iac.pipeline.sub_pipeline.duration"
    PIPELINE_SUB_STEP_DURATION = "iac.pipeline.sub_step.duration"
    PIPELINE_CANDIDATE_CANCELLED_COUNT = "iac.pipeline.candidate.cancelled.count"
    PIPELINE_USER_INPUT_WAIT_DURATION = "iac.pipeline.user_input.wait.duration"
    PIPELINE_CANDIDATE_COUNT = "iac.pipeline.candidate.count"
    PIPELINE_CANDIDATE_SUCCESS_COUNT = "iac.pipeline.candidate.success.count"
    PIPELINE_CANDIDATE_FAILED_COUNT = "iac.pipeline.candidate.failed.count"
    PIPELINE_FUNNEL_STEP_COUNT = "iac.pipeline.funnel.step.count"


class Spans:
    """Span name constants (ARMS LLM convention: '{operation} {identifier}')."""

    ENTRY = "enter_ai_application_system"
    LLM_CHAT = "chat"
    TOOL_EXECUTE = "execute_tool"
    REACT_STEP = "react step"
    SKILL_EXECUTE = "iac.skill.execute"
    TEMPLATE_VALIDATE = "iac.template.validate"

    # Pipeline
    PIPELINE_RUN = "iac.pipeline.run"
    PIPELINE_STEP = "iac.pipeline.step"
    PIPELINE_SUB_PIPELINE = "iac.pipeline.sub_pipeline"
    PIPELINE_SUB_STEP = "iac.pipeline.sub_step"
