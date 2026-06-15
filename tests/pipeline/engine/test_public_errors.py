import pytest

from iac_code.pipeline.engine.public_errors import public_error, sanitize_public_text


def test_public_error_redacts_bare_provider_keys_config_paths_and_uses_stable_id() -> None:
    first = public_error(
        message=(
            "Incorrect API key provided: sk-first-secret123 from ~/.iac-code/settings.yml "
            "and $HOME/.iac-code/.credentials.yml and /etc/iac-code/settings.yml"
        ),
        error_type="AuthenticationError",
    )
    second = public_error(
        message=(
            "Incorrect API key provided: sk-second-secret456 from ~/.iac-code/settings.yml "
            "and $HOME/.iac-code/.credentials.yml and /etc/iac-code/settings.yml"
        ),
        error_type="AuthenticationError",
    )

    rendered = str({"summary": first.summary, "details": first.details})
    assert "sk-first-secret123" not in rendered
    assert "~/.iac-code" not in rendered
    assert "$HOME/.iac-code" not in rendered
    assert "/etc/iac-code" not in rendered
    assert first.error_id == second.error_id


def test_public_error_redacts_prefixed_secrets_and_common_local_paths() -> None:
    failure = public_error(
        message=(
            "failed OPENAI_API_KEY=sk-live ALIYUN_ACCESS_KEY_SECRET=aliyun-secret "
            "DB_PASSWORD=hunter2 at /tmp/iac/file.py and /private/var/folders/aa/bb.py"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "sk-live" not in rendered
    assert "aliyun-secret" not in rendered
    assert "hunter2" not in rendered
    assert "/tmp/iac" not in rendered
    assert "/private/var/folders" not in rendered
    assert "OPENAI_API_KEY=[REDACTED]" in failure.summary
    assert "ALIYUN_ACCESS_KEY_SECRET=[REDACTED]" in failure.summary
    assert "DB_PASSWORD=[REDACTED]" in failure.summary
    assert "[PATH]" in failure.summary
    assert failure.details["traceback"] == "Stack trace omitted from public event; see error_id."
    assert failure.error_id


def test_public_error_redacts_windows_paths_with_spaces() -> None:
    failure = public_error(
        message=r"failed at C:\Users\Alice Smith\.iac-code\settings.yml and C:\Program Files\iac-code\config.yml",
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert r"C:\Users" not in rendered
    assert r"Alice Smith\.iac-code" not in rendered
    assert r"C:\Program Files" not in rendered
    assert "[PATH]" in failure.summary


def test_public_error_redacts_windows_forward_slash_and_unc_paths_with_spaces() -> None:
    failure = public_error(
        message=(
            "failed at C:/Users/Alice Smith/.iac-code/settings.yml and "
            r"\\server\share\Alice Smith\.iac-code\settings.yml and "
            "//server/share/Alice Smith/.iac-code/settings.yml"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "Alice Smith" not in rendered
    assert ".iac-code" not in rendered
    assert "server" not in rendered
    assert "//server/share" not in rendered
    assert "[PATH]" in failure.summary


def test_public_error_redacts_unix_paths_with_spaces() -> None:
    failure = public_error(
        message="failed at /Users/alice/My Project/.iac-code/settings.yml and /tmp/iac code/output.log",
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "My Project" not in rendered
    assert ".iac-code" not in rendered
    assert "iac code" not in rendered
    assert "[PATH]" in failure.summary


def test_public_error_redacts_path_without_swallowing_following_secret() -> None:
    failure = public_error(
        message="Duplicate session stored at /Users/alice/.iac-code/projects/session.json with api_key=sk-secret123",
        error_type="ValueError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "/Users/alice" not in rendered
    assert "sk-secret123" not in rendered
    assert "[PATH]" in failure.summary
    assert "api_key=[REDACTED]" in failure.summary


def test_public_error_redacts_encoded_local_paths_and_malformed_artifact_uris() -> None:
    encoded_file_uri = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"
    encoded_artifact_uri = (
        "iac-code-artifact%3A%2F%2Fartifact-1%2FC%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo%5Ctemplate.yaml"
    )

    failure = public_error(
        message=f"failed at {encoded_file_uri} and {encoded_artifact_uri}",
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "%2FUsers" not in rendered
    assert "%5CUsers" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "[PATH]" in failure.summary


def test_public_error_redacts_raw_file_and_artifact_uris_with_spaces() -> None:
    failure = public_error(
        message=(
            r"failed at file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml and next; "
            r"then iac-code-artifact://artifact-1/C:\Users\Alice and Bob\.iac-code\projects\demo\template.yaml "
            "with done"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "Alice and Bob" not in rendered
    assert ".iac-code" not in rendered
    assert "file://" not in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert failure.summary == "failed at [PATH]; then [PATH]"


def test_public_error_redacts_paths_before_newlines() -> None:
    failure = public_error(
        message=(
            "failed at file:///Users/Alice Smith/.iac-code/projects/demo/template.yaml\nnext line "
            "and /Users/Alice Smith/.iac-code/projects/demo/settings.yml\r\nmore"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "Alice Smith" not in rendered
    assert ".iac-code" not in rendered
    assert failure.summary == "failed at [PATH]\nnext line and [PATH]\r\nmore"


def test_public_error_redacts_connector_words_inside_final_filename() -> None:
    failure = public_error(
        message=(
            "failed at file:///Users/Alice Smith/.iac-code/projects/demo/template from prod.yaml and done "
            "plus /Users/Alice Smith/.iac-code/projects/demo/hello and world.yaml with ok"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "from prod.yaml" not in rendered
    assert "and world.yaml" not in rendered
    assert ".iac-code" not in rendered
    assert failure.summary == "failed at [PATH]"


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


def test_public_error_redacts_quoted_serialized_secret_values() -> None:
    failure = public_error(
        message='config {"api_key": "plain-secret", "nested": {"token": "tok-secret"}} '
        "and {'access_key_secret': 'aliyun-secret'}",
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "plain-secret" not in rendered
    assert "tok-secret" not in rendered
    assert "aliyun-secret" not in rendered
    assert "[REDACTED]" in failure.summary


def test_public_error_redacts_quoted_authorization_objects_with_secret_first() -> None:
    failure = public_error(
        message='headers {"Authorization": {"credentials": "bearer-secret-123", "scheme": "Bearer"}} '
        "and {'Authorization': {'credentials': 'other-secret-456', 'scheme': 'Bearer'}}",
        error_type="RuntimeError",
    )

    rendered = str({"summary": failure.summary, "details": failure.details})
    assert "bearer-secret-123" not in rendered
    assert "other-secret-456" not in rendered
    assert "[REDACTED]" in failure.summary


def test_public_error_redacts_auth_cookie_credentials_and_uses_sanitized_error_id() -> None:
    first = public_error(
        message=(
            "failed Authorization: Basic abc123 Cookie=session-cookie credentials=cred-secret "
            "private_key=key-secret session=sess-secret pwd=pwd-secret passwd=passwd-secret"
        ),
        error_type="RuntimeError",
    )
    second = public_error(
        message=(
            "failed Authorization: Basic def456 Cookie=other-cookie credentials=other-secret "
            "private_key=other-key session=other-session pwd=other-pwd passwd=other-passwd"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": first.summary, "details": first.details})
    for raw_secret in (
        "abc123",
        "session-cookie",
        "cred-secret",
        "key-secret",
        "sess-secret",
        "pwd-secret",
        "passwd-secret",
    ):
        assert raw_secret not in rendered
    assert "[REDACTED]" in first.summary
    assert first.error_id == second.error_id


def test_public_error_redacts_acs_authorization_cookie_lists_and_signed_query_values() -> None:
    first = public_error(
        message=(
            "failed Authorization: ACS3-HMAC-SHA256 Credential=LTAIabc123456789,Signature=secret-one "
            "Cookie: sid=first; refresh=second "
            "url=https://example.test/?Signature=query-secret&x-acs-security-token=query-token"
        ),
        error_type="RuntimeError",
    )
    second = public_error(
        message=(
            "failed Authorization: ACS3-HMAC-SHA256 Credential=LTAIdef123456789,Signature=secret-two "
            "Cookie: sid=third; refresh=fourth "
            "url=https://example.test/?Signature=other-secret&x-acs-security-token=other-token"
        ),
        error_type="RuntimeError",
    )

    rendered = str({"summary": first.summary, "details": first.details})
    for raw_secret in ("secret-one", "first", "second", "query-secret", "query-token", "LTAIabc123456789"):
        assert raw_secret not in rendered
    assert first.error_id == second.error_id
