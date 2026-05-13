"""Tests for ACPMetrics and metrics integration."""

import time

import pytest

from iac_code.acp.metrics import ACPMetrics


class TestACPMetrics:
    """Unit tests for the ACPMetrics dataclass."""

    def test_initial_state(self) -> None:
        m = ACPMetrics()
        assert m.total_sessions == 0
        assert m.active_sessions == 0
        assert m.total_prompts == 0
        assert m.total_errors == 0
        assert m.avg_prompt_duration_ms == 0.0

    def test_record_session_lifecycle(self) -> None:
        m = ACPMetrics()
        m.record_session_created()
        assert m.total_sessions == 1
        assert m.active_sessions == 1

        m.record_session_created()
        assert m.total_sessions == 2
        assert m.active_sessions == 2

        m.record_session_closed()
        assert m.active_sessions == 1
        assert m.total_sessions == 2  # total doesn't decrease

    def test_record_session_closed_floors_at_zero(self) -> None:
        m = ACPMetrics()
        m.record_session_closed()  # should not go negative
        assert m.active_sessions == 0

    def test_record_prompt(self) -> None:
        m = ACPMetrics()
        m.record_prompt(100.0)
        m.record_prompt(200.0)
        assert m.total_prompts == 2
        assert m.avg_prompt_duration_ms == 150.0

    def test_record_error(self) -> None:
        m = ACPMetrics()
        m.record_error()
        m.record_error()
        assert m.total_errors == 2

    def test_uptime_seconds(self) -> None:
        m = ACPMetrics()
        time.sleep(0.05)
        assert m.uptime_seconds >= 0.04

    def test_snapshot(self) -> None:
        m = ACPMetrics()
        m.record_session_created()
        m.record_prompt(42.5)
        snap = m.snapshot()
        assert snap["total_sessions"] == 1
        assert snap["active_sessions"] == 1
        assert snap["total_prompts"] == 1
        assert snap["total_errors"] == 0
        assert snap["avg_prompt_duration_ms"] == 42.5
        assert "uptime_seconds" in snap


class TestMetricsIntegration:
    """Test that ACPServer properly updates metrics."""

    @pytest.fixture
    def _patch_runtime(self, monkeypatch):
        """Patch create_agent_runtime for all tests in this class."""
        from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

        class FakeLoop:
            context_manager = None

            async def run_streaming(self, prompt):
                yield TextDeltaEvent(text="ok")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        class FakeRuntime:
            session_id = "test-session"
            agent_loop = FakeLoop()
            tool_registry = None

        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda options: FakeRuntime())

    @pytest.fixture
    def _fake_conn(self):
        class FakeConn:
            updates = []

            async def session_update(self, session_id, update, **kwargs):
                self.updates.append((session_id, update))

        return FakeConn()

    @pytest.mark.asyncio
    async def test_new_session_increments_metrics(self, monkeypatch, _patch_runtime, _fake_conn) -> None:
        import acp

        from iac_code.acp.server import ACPServer

        server = ACPServer()
        server.on_connect(_fake_conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        await server.new_session(cwd="/tmp")
        assert server.metrics.total_sessions == 1
        assert server.metrics.active_sessions == 1

    @pytest.mark.asyncio
    async def test_close_session_decrements_metrics(self, monkeypatch, _patch_runtime, _fake_conn) -> None:
        import acp

        from iac_code.acp.server import ACPServer

        server = ACPServer()
        server.on_connect(_fake_conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        resp = await server.new_session(cwd="/tmp")
        await server.close_session(session_id=resp.session_id)
        assert server.metrics.active_sessions == 0
        assert server.metrics.total_sessions == 1

    @pytest.mark.asyncio
    async def test_shutdown_all_sessions(self, monkeypatch, _patch_runtime, _fake_conn) -> None:
        import acp

        from iac_code.acp.server import ACPServer

        server = ACPServer()
        server.on_connect(_fake_conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        await server.new_session(cwd="/tmp")
        assert server.metrics.active_sessions == 1

        await server.shutdown_all_sessions()
        assert server.metrics.active_sessions == 0
        assert len(server.sessions) == 0

    @pytest.mark.asyncio
    async def test_prompt_records_duration(self, monkeypatch, _patch_runtime, _fake_conn) -> None:
        import acp

        from iac_code.acp.server import ACPServer

        server = ACPServer()
        server.on_connect(_fake_conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        resp = await server.new_session(cwd="/tmp")
        await server.prompt(
            session_id=resp.session_id,
            prompt=[acp.schema.TextContentBlock(type="text", text="hi")],
        )
        assert server.metrics.total_prompts == 1
        assert server.metrics.avg_prompt_duration_ms > 0
