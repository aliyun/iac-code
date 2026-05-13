"""Tests for the types/permissions module."""

from iac_code.types.permissions import PermissionMode, PermissionResult


class TestPermissionMode:
    """Tests for PermissionMode enum."""

    def test_permission_mode_values(self):
        """Test PermissionMode enum values."""
        assert PermissionMode.DEFAULT.value == "default"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.AUTO.value == "auto"

    def test_permission_mode_is_string_enum(self):
        """Test that PermissionMode is a string enum."""
        assert isinstance(PermissionMode.DEFAULT, str)
        assert PermissionMode.DEFAULT == "default"

    def test_all_permission_modes(self):
        """Test that all expected permission modes exist."""
        modes = {m.value for m in PermissionMode}
        assert modes == {"default", "plan", "auto"}


class TestPermissionResult:
    """Tests for PermissionResult dataclass."""

    def test_create_permission_result_allow(self):
        """Test creating an allow PermissionResult."""
        result = PermissionResult(behavior="allow")
        assert result.behavior == "allow"
        assert result.message == ""

    def test_create_permission_result_deny(self):
        """Test creating a deny PermissionResult."""
        result = PermissionResult(behavior="deny", message="Not allowed")
        assert result.behavior == "deny"
        assert result.message == "Not allowed"

    def test_create_permission_result_ask(self):
        """Test creating an ask PermissionResult."""
        result = PermissionResult(behavior="ask", message="Please confirm")
        assert result.behavior == "ask"
        assert result.message == "Please confirm"

    def test_permission_result_default_message(self):
        """Test PermissionResult default message is empty string."""
        result = PermissionResult(behavior="allow")
        assert result.message == ""
