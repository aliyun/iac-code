from __future__ import annotations

import pytest

from iac_code.agui.errors import AguiError
from iac_code.agui.inputs import parse_forwarded_props, resolve_cwd


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
