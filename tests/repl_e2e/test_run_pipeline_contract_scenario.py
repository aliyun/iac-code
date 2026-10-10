from __future__ import annotations

from scripts.repl.e2e.run_pipeline_contract_scenario import (
    _audit_pipeline_attribution,
    _expect_candidate_selection,
    _latest_pipeline_sidecar,
    parse_args,
)


def test_latest_pipeline_sidecar_reads_nested_session_layout(tmp_path) -> None:
    older = tmp_path / "projects" / "project" / "old" / "pipeline" / "meta.yaml"
    newer = tmp_path / "projects" / "project" / "new" / "pipeline" / "meta.yaml"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text("status: running\n", encoding="utf-8")
    newer.write_text("status: waiting_input\n", encoding="utf-8")

    path, payload = _latest_pipeline_sidecar(tmp_path)

    assert path == newer
    assert payload == {"status": "waiting_input"}


def test_candidate_selection_accepts_controls_or_terminal_readiness() -> None:
    seen = []

    class FakePty:
        transcript = ""

        def expect_any(self, patterns, *, description, timeout):
            seen.append((description, timeout, patterns))

    args = parse_args(["--timeout", "80"])
    _expect_candidate_selection(FakePty(), args, "selection")
    assert [item[0] for item in seen] == ["selection", "selection controls or input"]
    assert seen[1][1] == 80
    assert any("Press number keys" in pattern for pattern in seen[1][2])
    assert any("\\x1b" in pattern for pattern in seen[1][2])


def test_candidate_selection_accepts_controls_already_in_transcript() -> None:
    seen = []

    class FakePty:
        transcript = "Press number keys to select a candidate"

        def expect_any(self, patterns, *, description, timeout):
            seen.append(description)

    _expect_candidate_selection(FakePty(), parse_args([]), "selection")
    assert seen == ["selection"]


def test_pipeline_attribution_requires_steps_candidate_and_nudge() -> None:
    records = [
        _span(step_id="intent_parsing"),
        _span(step_id="architecture_planning"),
        _span(
            parent_step_id="evaluate_candidates",
            sub_pipeline_name="evaluate_candidate",
            sub_step_id="template_generating",
            candidate_index=0,
        ),
        _span(
            parent_step_id="evaluate_candidates",
            sub_pipeline_name="evaluate_candidate",
            sub_step_id="cost_estimating",
            candidate_index=0,
        ),
        _span(step_id="confirm_and_select"),
        {
            "kind": "log",
            "name": "iac.pipeline.step.nudged",
            "attributes": {"step_id": "intent_parsing"},
        },
    ]

    assert _audit_pipeline_attribution(records)["passed"] is True
    assert _audit_pipeline_attribution(records[:-2])["passed"] is False


def _span(**attributes) -> dict:
    return {"kind": "span", "name": "chat e2e-fixture-model", "attributes": attributes}
