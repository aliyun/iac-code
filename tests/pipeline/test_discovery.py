from unittest.mock import MagicMock

import pytest

from iac_code.pipeline import create_pipeline, discover_pipelines


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

    def test_unknown_pipeline_raises(self):
        with pytest.raises(ValueError, match="Unknown pipeline"):
            create_pipeline(
                "nonexistent",
                provider_manager=MagicMock(),
                base_tool_registry=MagicMock(),
                session_storage=MagicMock(),
                session_id="test",
            )
