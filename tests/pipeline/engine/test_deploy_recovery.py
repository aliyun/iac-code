from iac_code.pipeline.engine.deploy_recovery import (
    DeployAttemptEvidence,
    failed_deploy_attempts,
    validate_deployment_recovery,
)


def _record(action, *, is_error=False, is_success=None, status="", status_reason="", stack_id="stack-1"):
    result = {"stack_id": stack_id, "status": status, "status_reason": status_reason}
    if is_success is not None:
        result["is_success"] = is_success
    return {
        "tool_name": "ros_deploy",
        "input": {"action": action},
        "result": result,
        "is_error": is_error,
    }


class TestFailedDeployAttempts:
    def test_no_records_yields_no_attempts(self):
        assert failed_deploy_attempts(None) == []
        assert failed_deploy_attempts([]) == []

    def test_successful_create_is_not_an_attempt(self):
        records = [_record("create", is_success=True, status="CREATE_COMPLETE")]

        assert failed_deploy_attempts(records) == []

    def test_wait_action_is_ignored(self):
        records = [_record("wait", is_success=False, status="CREATE_FAILED")]

        assert failed_deploy_attempts(records) == []

    def test_non_ros_deploy_tools_are_ignored(self):
        records = [
            {
                "tool_name": "ros_validate_template",
                "input": {"action": "create"},
                "result": {"status": "CREATE_FAILED"},
                "is_error": True,
            }
        ]

        assert failed_deploy_attempts(records) == []

    def test_collects_failures_in_order(self):
        records = [
            _record("create", is_success=False, status="CREATE_FAILED", status_reason="overlapped cidr"),
            {
                "tool_name": "edit_file",
                "input": {"path": "template.yml"},
                "result": {"file_path": "template.yml"},
                "is_error": False,
            },
            _record("continue_create", is_error=True, status="CREATE_FAILED", status_reason="zone mismatch"),
            _record("continue_create", is_success=True, status="CREATE_COMPLETE"),
        ]

        attempts = failed_deploy_attempts(records)

        assert attempts == [
            DeployAttemptEvidence("create", "stack-1", "CREATE_FAILED", "overlapped cidr"),
            DeployAttemptEvidence("continue_create", "stack-1", "CREATE_FAILED", "zone mismatch"),
        ]

    def test_falls_back_to_message_when_status_reason_is_missing(self):
        records = [
            {
                "tool_name": "ros_deploy",
                "input": {"action": "continue_create"},
                "result": {"stack_id": "stack-1", "message": "ContinueCreateStackValidationFailed"},
                "is_error": True,
            }
        ]

        assert failed_deploy_attempts(records)[0].reason == "ContinueCreateStackValidationFailed"


class TestValidateDeploymentRecovery:
    attempts = [
        DeployAttemptEvidence("create", "stack-1", "CREATE_FAILED", "overlapped cidr"),
        DeployAttemptEvidence("continue_create", "stack-1", "CREATE_FAILED", "zone mismatch"),
    ]

    def _recovery(self, **overrides):
        recovery = {
            "retry_count": 2,
            "failed_attempts": [
                {
                    "action": "create",
                    "stack_id": "stack-1",
                    "status": "CREATE_FAILED",
                    "reason": "VPC CidrBlock overlapped",
                },
                {
                    "action": "continue_create",
                    "stack_id": "stack-1",
                    "status": "CREATE_FAILED",
                    "reason": "VSwitch zone mismatch",
                },
            ],
            "recovery_path": "create CREATE_FAILED -> edit_file 修模板 -> ros_validate_template -> continue_create OK",
        }
        recovery.update(overrides)
        return recovery

    def test_no_attempts_requires_nothing(self):
        assert validate_deployment_recovery(None, []) is None

    def test_consistent_record_passes(self):
        assert validate_deployment_recovery(self._recovery(), self.attempts) is None

    def test_missing_record_is_rejected(self):
        error = validate_deployment_recovery(None, self.attempts)

        assert error is not None
        assert "deployment_recovery is required" in error
        assert "2 time(s)" in error
        assert "CREATE_FAILED" in error

    def test_retry_count_must_match_evidence(self):
        error = validate_deployment_recovery(self._recovery(retry_count=1), self.attempts)

        assert error is not None
        assert "retry_count must be 2" in error

    def test_boolean_retry_count_is_rejected(self):
        error = validate_deployment_recovery(self._recovery(retry_count=True), self.attempts)

        assert error is not None
        assert "retry_count must be 2" in error

    def test_failed_attempts_count_must_match_evidence(self):
        recovery = self._recovery()
        recovery["failed_attempts"] = recovery["failed_attempts"][:1]

        error = validate_deployment_recovery(recovery, self.attempts)

        assert error is not None
        assert "failed_attempts must contain exactly 2 entries" in error

    def test_empty_reason_is_rejected(self):
        recovery = self._recovery()
        recovery["failed_attempts"][1]["reason"] = "  "

        error = validate_deployment_recovery(recovery, self.attempts)

        assert error is not None
        assert "failed_attempts[1].reason" in error

    def test_action_must_match_evidence(self):
        recovery = self._recovery()
        recovery["failed_attempts"][1]["action"] = "delete_and_create"

        error = validate_deployment_recovery(recovery, self.attempts)

        assert error is not None
        assert "failed_attempts[1].action must be continue_create" in error

    def test_status_must_match_evidence(self):
        recovery = self._recovery()
        recovery["failed_attempts"][0]["status"] = "CREATE_COMPLETE"

        error = validate_deployment_recovery(recovery, self.attempts)

        assert error is not None
        assert "failed_attempts[0].status must be CREATE_FAILED" in error

    def test_unobserved_fields_are_not_enforced(self):
        attempts = [DeployAttemptEvidence("create", "", "", "")]
        recovery = {
            "retry_count": 1,
            "failed_attempts": [{"action": "create", "reason": "quota exceeded"}],
            "recovery_path": "create 失败 -> 提额 -> create 成功",
        }

        assert validate_deployment_recovery(recovery, attempts) is None

    def test_recovery_path_is_required(self):
        error = validate_deployment_recovery(self._recovery(recovery_path=""), self.attempts)

        assert error is not None
        assert "recovery_path must describe" in error
