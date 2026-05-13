from iac_code.tools.cloud.types import InstanceStatus, ResourceStatus, StackStatus


class TestStackStatus:
    def _make(self, status: str) -> StackStatus:
        return StackStatus(
            stack_id="stack-001",
            stack_name="my-stack",
            status=status,
            status_reason="",
            progress_percentage=0,
        )

    def test_is_terminal_complete(self):
        assert self._make("CREATE_COMPLETE").is_terminal is True

    def test_is_terminal_failed(self):
        assert self._make("CREATE_FAILED").is_terminal is True

    def test_is_terminal_in_progress(self):
        assert self._make("CREATE_IN_PROGRESS").is_terminal is False

    def test_is_success_complete(self):
        assert self._make("CREATE_COMPLETE").is_success is True

    def test_is_success_failed(self):
        assert self._make("CREATE_FAILED").is_success is False


class TestResourceStatus:
    def _make(self, status: str) -> ResourceStatus:
        return ResourceStatus(
            name="my-resource",
            resource_type="ALIYUN::ECS::Instance",
            status=status,
            status_reason="",
        )

    def test_status_icon_complete(self):
        assert self._make("CREATE_COMPLETE").status_icon == "✅"

    def test_status_icon_in_progress(self):
        assert self._make("CREATE_IN_PROGRESS").status_icon == "⏳"

    def test_status_icon_failed(self):
        assert self._make("CREATE_FAILED").status_icon == "❌"

    def test_status_icon_pending(self):
        assert self._make("PENDING").status_icon == "⬚"


class TestInstanceStatus:
    def _make(self, status: str) -> InstanceStatus:
        return InstanceStatus(
            account_id="123456",
            region_id="cn-hangzhou",
            status=status,
            status_reason="",
            elapsed_seconds=0,
        )

    def test_status_icon_succeeded(self):
        assert self._make("SUCCEEDED").status_icon == "✅"

    def test_status_icon_running(self):
        assert self._make("RUNNING").status_icon == "⏳"

    def test_status_icon_failed(self):
        assert self._make("FAILED").status_icon == "❌"

    def test_status_icon_pending(self):
        assert self._make("PENDING").status_icon == "⬚"
