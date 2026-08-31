from __future__ import annotations

from types import SimpleNamespace

import pytest
from ag_ui.core import ImageInputContent, InputContentDataSource, RunAgentInput, UserMessage

from iac_code.agui.errors import AguiError
from iac_code.agui.inputs import latest_user_message, parse_forwarded_props, resolve_cwd


def test_forwarded_props_require_request_workspace_and_identity(tmp_path) -> None:
    props = parse_forwarded_props(
        {
            "iacCode": {
                "schemaVersion": 1,
                "rosInvocationId": "invocation-1",
                "cwd": str(tmp_path),
                "model": "qwen-test",
                "runMode": "pipeline",
            }
        }
    )

    assert props.iac_code.cwd == str(tmp_path)
    assert props.iac_code.model == "qwen-test"
    assert props.iac_code.run_mode == "pipeline"


def test_cwd_is_checked_against_adapter_roots(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(allowed))

    assert resolve_cwd(str(allowed / "session")) == str((allowed / "session").resolve())
    with pytest.raises(AguiError, match="outside the allowed roots"):
        resolve_cwd(str(tmp_path / "outside"))


@pytest.mark.parametrize(
    ("metadata", "expected_filename"),
    [
        ({"filename": "before.png"}, "before.png"),
        ({"filename": "../before.png"}, "agui-image-1"),
        ({"filename": "CON"}, "agui-image-1"),
        (None, "agui-image-1"),
    ],
)
def test_latest_user_message_uses_safe_image_metadata_filename(
    metadata,
    expected_filename,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "iac_code.agui.inputs.maybe_resize_and_downsample",
        lambda _raw: SimpleNamespace(data=b"resized", media_type="image/png"),
    )
    run_input = RunAgentInput(
        thread_id="thread-1",
        run_id="run-1",
        state={},
        messages=[
            UserMessage(
                id="message-1",
                content=[
                    ImageInputContent(
                        source=InputContentDataSource(
                            value="YWJj",
                            mime_type="image/png",
                        ),
                        metadata=metadata,
                    )
                ],
            )
        ],
        tools=[],
        context=[],
        forwarded_props={},
    )

    _message_id, parts = latest_user_message(run_input)

    assert parts[0]["data"]["filename"] == expected_filename
