from __future__ import annotations

import textwrap

from iac_code.pipeline.engine.loader import load_pipeline_dir


def test_loader_binds_cleanup_hook_functions(tmp_path) -> None:
    (tmp_path / "pipeline.yaml").write_text(
        textwrap.dedent(
            """
            name: cleanup-hooks
            context_dependencies:
              deployment: []
            steps:
              - id: deploying
                conclusion_field: deployment
                forward: null
                prompt: deploying.md
                hooks_file: hooks/deploying.py
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "deploying.md").write_text("deploy", encoding="utf-8")
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "deploying.py").write_text(
        textwrap.dedent(
            """
            def on_resource_observed(*args, **kwargs):
                return None

            def on_rollback_cleanup_required(*args, **kwargs):
                return []
            """
        ),
        encoding="utf-8",
    )

    loaded = load_pipeline_dir(tmp_path)
    [step] = loaded.steps

    assert step.on_resource_observed is not None
    assert step.on_resource_observed.__name__ == "on_resource_observed"
    assert step.on_rollback_cleanup_required is not None
    assert step.on_rollback_cleanup_required.__name__ == "on_rollback_cleanup_required"
