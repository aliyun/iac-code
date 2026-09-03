"""Low-dependency pipeline constants."""

SELLING_PIPELINE_NAME = "selling"
SELLING_SOLUTION_FIRST_PIPELINE_NAME = "selling_solution_first"

#: Pipelines a remote caller (POP → ros-ai-agent → A2A) may select per request.
SELECTABLE_PIPELINE_NAMES: frozenset[str] = frozenset({SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME})

CLEANUP_PROMPT_METADATA_TYPE = "pipeline_cleanup_prompt"

PIPELINE_EVENT_CLEANUP_STARTED = "cleanup_started"
PIPELINE_EVENT_CLEANUP_PROGRESS = "cleanup_progress"
PIPELINE_EVENT_CLEANUP_COMPLETED = "cleanup_completed"
PIPELINE_EVENT_CLEANUP_FAILED = "cleanup_failed"
