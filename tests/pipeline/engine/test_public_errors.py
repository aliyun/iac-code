import pytest

from iac_code.pipeline.engine.public_errors import public_error, sanitize_public_text


@pytest.mark.parametrize(
    "message",
    [
        "Incorrect API key provided: sk-first-secret123 from ~/.iac-code/settings.yml",
        "failed OPENAI_API_KEY=sk-live DB_PASSWORD=hunter2 at /tmp/iac/file.py",
        r"failed at C:\Users\Alice Smith\.iac-code\settings.yml",
        r"failed at \\server\share\Alice Smith\.iac-code\settings.yml",
        "failed at /Users/alice/My Project/.iac-code/settings.yml",
        "failed at file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Ftemplate.yaml",
        r"failed at file:///Users/Alice and Bob/.iac-code/template.yaml",
    ],
)
def test_pipeline_public_error_preserves_canonical_message(message: str) -> None:
    failure = public_error(message=message, error_type="RuntimeError")

    assert failure.summary == message
    assert failure.details["type"] == "RuntimeError"
    assert failure.details["traceback"] == "Stack trace omitted from public event; see error_id."
    assert failure.error_id
    assert "[PATH]" not in failure.summary
    assert "[REDACTED]" not in failure.summary


def test_pipeline_public_error_id_uses_the_raw_canonical_message() -> None:
    first = public_error(message="token=first", error_type="RuntimeError")
    second = public_error(message="token=second", error_type="RuntimeError")

    assert first.error_id != second.error_id


@pytest.mark.parametrize(
    "uri",
    [
        "iac-code-artifact://artifact-1/CON.txt",
        "iac-code-artifact://artifact-1/LPT%C2%B3.log",
        "iac-code-artifact://artifact-1/bad%3Aname.yaml",
        "iac-code-artifact://artifact-1/name%20.",
        "iac-code-artifact://artifact-1/%01.txt",
    ],
)
def test_sanitize_public_text_redacts_windows_unsafe_artifact_uris(uri: str) -> None:
    sanitized = sanitize_public_text(f"see {uri}")

    assert sanitized == "see [PATH]"
    assert uri not in sanitized


def test_sanitize_public_text_preserves_valid_artifact_uri_with_trailing_punctuation() -> None:
    uri = "iac-code-artifact://artifact-1/template.yaml"

    assert sanitize_public_text(f"see {uri}.") == f"see {uri}."


def test_sanitize_public_text_preserves_valid_artifact_uri_before_prose() -> None:
    uri = "iac-code-artifact://artifact-1/template.yaml"

    assert sanitize_public_text(f"see {uri} and next") == f"see {uri} and next"


def test_sanitize_public_text_preserves_normal_https_urls() -> None:
    assert sanitize_public_text("see https://example.test/doc") == "see https://example.test/doc"


def test_pipeline_public_error_preserves_structured_extra_details() -> None:
    extra = {
        "credentials": {"Authorization": "Bearer secret-value"},
        "path": "/Users/alice/.iac-code/settings.yml",
    }

    failure = public_error(message="failed", error_type="RuntimeError", extra_details=extra)

    assert failure.details["credentials"] == extra["credentials"]
    assert failure.details["path"] == extra["path"]
