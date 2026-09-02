from __future__ import annotations

from dataclasses import dataclass

from iac_code.i18n import SUPPORTED_LANGUAGES, translate_message

# Keep the adapter's stable public error messages in the messages catalog while
# still translating them per request.  Calling ``translate_message`` with
# English at import time is a no-op; its literal arguments are also extracted by
# Babel, and the allowlist prevents accidentally returning arbitrary upstream
# details to a caller in an unrelated locale.
_PUBLIC_AGUI_ERROR_MESSAGES = frozenset(
    {
        translate_message("A new run requires a user message.", language="en"),
        translate_message("A resolved interrupt requires a payload.", language="en"),
        translate_message("An image exceeds the maximum size.", language="en"),
        translate_message("An interrupt belongs to another A2A context.", language="en"),
        translate_message("An interrupt belongs to another A2A task.", language="en"),
        translate_message("Client-provided tools are not supported.", language="en"),
        translate_message("Invalid iac-code forwarded properties.", language="en"),
        translate_message("Only text and inline data images are supported.", language="en"),
        translate_message("Remote media URLs are not supported.", language="en"),
        translate_message(
            "Session backup is still synchronizing. Retry after 3 seconds.",
            language="en",
        ),
        translate_message("The A2A context identity changed unexpectedly.", language="en"),
        translate_message("The A2A execution failed.", language="en"),
        translate_message("The A2A interrupt response was not accepted.", language="en"),
        translate_message("The A2A permission response was not accepted.", language="en"),
        translate_message("The A2A task identity changed unexpectedly.", language="en"),
        translate_message("The A2A task identity does not match the interrupted run.", language="en"),
        translate_message("The A2A task to resume is unavailable.", language="en"),
        translate_message("The AG-UI adapter state is unavailable.", language="en"),
        translate_message("The AG-UI run id has already been used.", language="en"),
        translate_message("The AG-UI thread already has an active run.", language="en"),
        translate_message(
            "The AG-UI thread is already bound to another workspace or caller.",
            language="en",
        ),
        translate_message("The AG-UI thread is waiting for interrupt responses.", language="en"),
        translate_message("The accepted interrupt response could not be committed.", language="en"),
        translate_message("The execution mapping could not be committed.", language="en"),
        translate_message("The execution session mapping could not be committed.", language="en"),
        translate_message("The execution to resume is no longer available.", language="en"),
        translate_message("The execution was cancelled by the interrupt response.", language="en"),
        translate_message("The execution was cancelled.", language="en"),
        translate_message("The iac-code session identity changed unexpectedly.", language="en"),
        translate_message("The iac-code session to resume is unavailable.", language="en"),
        translate_message("The iac-code workspace cannot be created.", language="en"),
        translate_message("The iac-code workspace is invalid.", language="en"),
        translate_message("The iac-code workspace is not a directory.", language="en"),
        translate_message("The iac-code workspace is outside the allowed roots.", language="en"),
        translate_message("The iac-code workspace must be an absolute path.", language="en"),
        translate_message("The image data is invalid.", language="en"),
        translate_message("The image data is not valid base64.", language="en"),
        translate_message("The image media type is not supported.", language="en"),
        translate_message("The interrupt response does not contain an answer.", language="en"),
        translate_message("The interrupt response has already been applied.", language="en"),
        translate_message("The interrupt response payload is invalid.", language="en"),
        translate_message("The interrupted execution state could not be committed.", language="en"),
        translate_message("The local A2A execution service is unavailable.", language="en"),
        translate_message(
            "The local A2A execution service rejected the interrupt response.",
            language="en",
        ),
        translate_message("The resume contains duplicate interrupt ids.", language="en"),
        translate_message(
            "The resume must resolve every pending interrupt exactly once.",
            language="en",
        ),
        translate_message("The resume references an unknown interrupt.", language="en"),
        translate_message("The resume request does not match the interrupted run.", language="en"),
        translate_message("The total image content exceeds the maximum size.", language="en"),
        translate_message("The user message content is invalid.", language="en"),
    }
)


def translate_agui_error(message: str, *, language: str) -> str:
    """Translate one stable public adapter error without process-global locale state."""

    if message not in _PUBLIC_AGUI_ERROR_MESSAGES:
        return message
    return translate_message(message, language=normalize_agui_language(language))


def normalize_agui_language(value: object, *, fallback: str = "en") -> str:
    """Resolve a language tag to one supported request-local messages catalog."""

    if isinstance(value, str):
        language = value.strip().lower().replace("_", "-").split("-", 1)[0]
        if language in SUPPORTED_LANGUAGES:
            return language
    return fallback


@dataclass(frozen=True)
class AguiError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AdmissionError(Exception):
    code: str
    message: str
    status_code: int = 409

    def __str__(self) -> str:
        return self.message
