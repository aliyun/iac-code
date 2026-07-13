from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from iac_code.mcp.types import MCPConnectionState
from iac_code.pipeline import create_pipeline, discover_pipelines
from iac_code.pipeline.engine.events import PipelineEventType


class TestDiscoverPipelines:
    def test_discovers_selling(self):
        pipelines = discover_pipelines()
        assert "selling" in pipelines
        assert (pipelines["selling"] / "pipeline.yaml").exists()

    def test_engine_not_discovered(self):
        pipelines = discover_pipelines()
        assert "engine" not in pipelines


class TestCreatePipeline:
    def test_creates_selling_pipeline(self):
        storage = MagicMock()
        storage.session_path.return_value = MagicMock()
        pipeline = create_pipeline(
            "selling",
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )
        assert pipeline.pipeline_name == "selling"
        assert pipeline.state_machine.total_steps == 5

    def test_create_pipeline_propagates_mcp_status_metadata_to_runner(self):
        storage = MagicMock()
        storage.session_path.return_value = MagicMock()
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="broken",
                    state=MCPConnectionState.FAILED,
                    error="access_token=super-secret-token",
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=1,
                    metadata=None,
                )
            ]
        )
        warnings = [SimpleNamespace(server_name="broken", code="connection_failed", message="MCP server failed")]

        pipeline = create_pipeline(
            "selling",
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
            mcp_manager=manager,
            mcp_config_warnings=warnings,
        )

        event = pipeline._mcp_status_event()
        assert event is not None
        assert event.type is PipelineEventType.MCP_STATUS
        assert event.data["mcp_status"]["servers"][0]["serverName"] == "broken"
        assert event.data["mcp_status"]["warnings"][0]["code"] == "connection_failed"
        assert "super-secret-token" not in repr(event.data["mcp_status"])

    def test_unknown_pipeline_raises(self):
        with pytest.raises(ValueError, match="Unknown pipeline"):
            create_pipeline(
                "nonexistent",
                provider_manager=MagicMock(),
                base_tool_registry=MagicMock(),
                session_storage=MagicMock(),
                session_id="test",
            )

    def test_passes_prerequisite_resolution_to_runner(self, monkeypatch, tmp_path):
        import iac_code.pipeline.engine.pipeline_runner as runner_module

        pipeline_dir = tmp_path / "pipeline"
        pipeline_dir.mkdir()
        runner = MagicMock()
        runner_cls = MagicMock(return_value=runner)
        prerequisite_resolution = {
            "feature_flags": {"enable_reviewing": False},
            "decisions": {"infraguard": {"status": "missing"}},
            "env_overrides": {},
        }

        monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"selling": pipeline_dir})
        monkeypatch.setattr(runner_module, "PipelineRunner", runner_cls)

        result = create_pipeline(
            "selling",
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=MagicMock(),
            session_id="test123",
            prerequisite_resolution=prerequisite_resolution,
        )

        assert result is runner
        assert runner_cls.call_args.kwargs["prerequisite_resolution"] is prerequisite_resolution

    def test_resume_from_sidecar_peeks_prerequisites_before_runner_creation(self, monkeypatch, tmp_path):
        import iac_code.pipeline.engine.pipeline_runner as runner_module

        pipeline_dir = tmp_path / "pipeline"
        pipeline_dir.mkdir()
        storage = MagicMock()
        storage.session_dir.side_effect = lambda cwd, sid: tmp_path / "sessions" / sid
        sidecar_dir = storage.session_dir("/workspace", "sess123") / "pipeline"
        sidecar_dir.mkdir(parents=True)
        prerequisites = {
            "feature_flags": {"enable_reviewing": False},
            "decisions": {"infraguard": {"status": "missing"}},
            "env_overrides": {"PATH": "/tmp/tools"},
        }
        (sidecar_dir / "meta.yaml").write_text(
            yaml.dump({"status": "running", "prerequisites": prerequisites}),
            encoding="utf-8",
        )
        runner_cls = MagicMock(return_value=MagicMock())

        monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"selling": pipeline_dir})
        monkeypatch.setattr(runner_module, "PipelineRunner", runner_cls)

        create_pipeline(
            "selling",
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="sess123",
            cwd="/workspace",
            resume_from_sidecar=True,
        )

        assert runner_cls.call_args.kwargs["prerequisite_resolution"] == prerequisites
