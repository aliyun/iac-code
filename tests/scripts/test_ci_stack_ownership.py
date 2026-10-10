from pathlib import Path

import pytest
import yaml

from scripts.ci.stack_ownership import case_pipeline_dirs, creation_receipts


def write_receipt(directory: Path, *, action: str = "CreateStack", name: str = "model-chosen-network") -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    resource = {
        "provider": "ros", "resource_type": "stack", "resource_id": "owned-stack-id",
        "resource_name": name, "region_id": "cn-hangzhou", "source_step_id": "deploying",
        "source_attempt_id": "att_001", "observed_action": action,
        "metadata": {"tool_name": "ros_stack", "tool_use_id": "tool-create-1"},
    }
    (directory / "cleanup.yaml").write_text(yaml.safe_dump({"observed_resources": [resource]}), encoding="utf-8")
    (directory / "meta.yaml").write_text(yaml.safe_dump({
        "attempts": {"items": {"att_001": {"step_id": "deploying"}}},
    }), encoding="utf-8")
    return resource


def test_creation_receipt_accepts_application_chosen_name_and_deduplicates(tmp_path):
    write_receipt(tmp_path)
    receipts = creation_receipts([tmp_path, tmp_path])
    assert len(receipts) == 1
    assert receipts[0]["stackName"] == "model-chosen-network"
    assert receipts[0]["ownershipSource"] == "accepted_create_ledger"


@pytest.mark.parametrize("action", ["ContinueCreateStack", "GetStack", "wait", "DeleteStack"])
def test_observation_of_existing_stack_never_authorizes_deletion(tmp_path, action):
    write_receipt(tmp_path, action=action)
    assert creation_receipts([tmp_path]) == []


@pytest.mark.parametrize("field", ["resource_name", "region_id", "source_attempt_id", "metadata"])
def test_incomplete_receipt_fails_closed(tmp_path, field):
    resource = write_receipt(tmp_path)
    resource.pop(field)
    (tmp_path / "cleanup.yaml").write_text(yaml.safe_dump({"observed_resources": [resource]}), encoding="utf-8")
    with pytest.raises(ValueError):
        creation_receipts([tmp_path])


def test_attempt_from_another_step_cannot_authorize_deletion(tmp_path):
    write_receipt(tmp_path)
    (tmp_path / "meta.yaml").write_text(yaml.safe_dump({
        "attempts": {"items": {"att_001": {"step_id": "confirm_and_select"}}},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="does not belong"):
        creation_receipts([tmp_path])


def test_other_case_sessions_and_agent_local_config_are_not_read(tmp_path, monkeypatch):
    from iac_code.services.session_storage import SessionStorage

    config = tmp_path / "case-config"
    storage = SessionStorage(projects_dir=config / "projects")
    ours = storage.session_dir(str(tmp_path / "case-workspace"), "ours") / "pipeline"
    other = storage.session_dir(str(tmp_path / "other-workspace"), "foreign") / "pipeline"
    write_receipt(ours)
    write_receipt(other, name="foreign-name")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "agent-home"))
    assert case_pipeline_dirs(config, str(tmp_path / "case-workspace")) == [ours]
    assert not (tmp_path / "agent-home").exists()
