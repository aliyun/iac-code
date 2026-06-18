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

    def test_legacy_windows_success_icon_uses_ascii(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.components.status_icon.use_ascii_symbols", lambda: True)
        icon = StatusIcon(Status.SUCCESS)
        result = icon.render()
        assert result.plain == "OK"

    def test_legacy_windows_all_icons_are_ascii(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.components.status_icon.use_ascii_symbols", lambda: True)

        rendered = {status: StatusIcon(status).render().plain for status in Status}

        assert rendered == {
            Status.SUCCESS: "OK",
            Status.ERROR: "X",
            Status.WARNING: "!",
            Status.INFO: "i",
            Status.PENDING: ".",
            Status.RUNNING: "*",
        }

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
