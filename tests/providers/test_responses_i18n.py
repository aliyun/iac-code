import ast
import gettext
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pytest
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from google.protobuf.json_format import MessageToDict

import iac_code.i18n as i18n
from iac_code.a2a.events import publish_stream_event
from iac_code.a2a.runtime_overrides import a2a_request_context
from iac_code.providers.manager import _error_event_from_exception
from iac_code.providers.responses_codec import (
    ResponsesConfigurationError,
    ResponsesContextLimitError,
    ResponsesProtocolError,
)
from iac_code.providers.responses_provider import validate_responses_endpoint
from tests.a2a.fakes import FakeEventQueue

ROOT = Path(__file__).resolve().parents[2]
ERROR_CASES = [
    (ResponsesConfigurationError, "Model apiMode must be chat_completions or responses."),
    (ResponsesProtocolError, "Responses function arguments are not complete JSON."),
    (ResponsesContextLimitError, "Responses input exceeds the model context limit."),
]


def _feature_messages():
    messages = {"Responses input exceeds the safe context budget after local compaction."}
    for filename in ("responses_codec.py", "responses_provider.py", "openai_provider.py", "manager.py"):
        source = ROOT / "src/iac_code/providers" / filename
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id
                in {"ResponsesConfigurationError", "ResponsesProtocolError", "ResponsesContextLimitError"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                messages.add(node.args[0].value)
    return messages


@lru_cache(maxsize=None)
def _catalog(language):
    path = ROOT / "src/iac_code/i18n/locales" / language / "LC_MESSAGES/messages.po"
    with path.open("rb") as file:
        return read_po(file)


@pytest.fixture
def compiled_catalogs(monkeypatch):
    translations = {}
    for language in i18n.SUPPORTED_LANGUAGES:
        if language == "en":
            continue
        output = BytesIO()
        write_mo(output, _catalog(language))
        output.seek(0)
        translations[language] = gettext.GNUTranslations(output)
    monkeypatch.setattr(i18n, "_messages_catalog_cache", translations)


@pytest.mark.parametrize("language", [lang for lang in i18n.SUPPORTED_LANGUAGES if lang != "en"])
def test_every_responses_user_message_has_a_complete_translation(language):
    catalog = _catalog(language)
    messages = _feature_messages()
    assert messages
    for message_id in messages:
        message = catalog.get(message_id)
        assert message is not None, message_id
        assert message.string and not message.fuzzy, message_id
        assert message.string != message_id, message_id


@pytest.mark.parametrize("language", i18n.SUPPORTED_LANGUAGES)
@pytest.mark.parametrize("error_class,message_id", ERROR_CASES)
def test_responses_errors_localize_and_keep_message_ids(compiled_catalogs, language, error_class, message_id):
    with i18n.use_request_language(language):
        error = error_class(message_id)
    assert str(error) == i18n.translate_message(message_id, language=language)
    event = _error_event_from_exception(error)
    assert event.i18n_message_id == message_id
    assert event.context_limit_exceeded == (error_class is ResponsesContextLimitError)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", i18n.SUPPORTED_LANGUAGES)
@pytest.mark.parametrize("error_class,message_id", ERROR_CASES)
async def test_responses_a2a_errors_use_the_callers_language(compiled_catalogs, language, error_class, message_id):
    with i18n.use_request_language("zh"):
        event = _error_event_from_exception(error_class(message_id))
    queue = FakeEventQueue()
    with a2a_request_context(preferred_language=language):
        await publish_stream_event(queue, task_id="test-task", context_id="test-context", event=event)
    payload = MessageToDict(queue.events[0], preserving_proto_field_name=False)
    assert payload["status"]["message"]["parts"][0]["text"] == i18n.translate_message(message_id, language=language)


@pytest.mark.parametrize(
    "base_url", ["https://[invalid/v1", "https://api.openai.com:bad/v1", "https://api.openai.com:99999/v1"]
)
def test_malformed_responses_urls_have_a_localizable_configuration_error(base_url):
    with pytest.raises(ResponsesConfigurationError) as raised:
        validate_responses_endpoint(base_url)
    assert raised.value.i18n_message_id == "Responses API requires a valid HTTP or HTTPS base URL."
