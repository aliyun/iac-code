"""Tests for StatusIcon component."""

from rich.text import Text

from iac_code.ui.components.status_icon import Status, StatusIcon


class TestStatusIcon:
    def test_render_returns_text(self):
        icon = StatusIcon(Status.SUCCESS)
        result = icon.render()
        assert isinstance(result, Text)

    def test_success_icon(self):
        icon = StatusIcon(Status.SUCCESS)
        result = icon.render()
        assert "✓" in result.plain

    def test_error_icon(self):
        icon = StatusIcon(Status.ERROR)
        result = icon.render()
        assert "✗" in result.plain

    def test_warning_icon(self):
        icon = StatusIcon(Status.WARNING)
        result = icon.render()
        assert "⚠" in result.plain

    def test_info_icon(self):
        icon = StatusIcon(Status.INFO)
        result = icon.render()
        assert "●" in result.plain

    def test_pending_icon(self):
        icon = StatusIcon(Status.PENDING)
        result = icon.render()
        assert "○" in result.plain

    def test_running_icon(self):
        icon = StatusIcon(Status.RUNNING)
        result = icon.render()
        assert "◐" in result.plain

    def test_all_statuses_renderable(self):
        for status in Status:
            icon = StatusIcon(status)
            result = icon.render()
            assert isinstance(result, Text)
            assert len(result.plain) > 0

    def test_status_enum_values(self):
        assert Status.SUCCESS.value == "success"
        assert Status.ERROR.value == "error"
        assert Status.WARNING.value == "warning"
        assert Status.INFO.value == "info"
        assert Status.PENDING.value == "pending"
        assert Status.RUNNING.value == "running"
