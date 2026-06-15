from scripts.observability.local_observe.assertions import evaluate_assertions


def test_raw_content_on_requires_prompt_fields():
    records = [{"id": "entry", "kind": "span", "name": "enter_ai_application_system", "attributes": {}}]

    result = evaluate_assertions(records, expected_raw_content="on")

    failed = [item for item in result if item["status"] == "fail"]
    assert any("gen_ai.input.messages" in item["label"] for item in failed)


def test_raw_content_off_flags_prompt_leak():
    records = [
        {
            "id": "entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "attributes": {"gen_ai.input.messages": "[raw prompt]"},
        }
    ]

    result = evaluate_assertions(records, expected_raw_content="off")

    assert any(item["status"] == "fail" and item["evidence_ids"] == ["entry"] for item in result)


def test_step_attempt_assertion_passes_when_repeated_step_has_distinct_attempts():
    records = [
        {
            "id": "a1",
            "kind": "span",
            "name": "iac.pipeline.step",
            "attributes": {"step_id": "template", "step_attempt": 1},
        },
        {
            "id": "a2",
            "kind": "span",
            "name": "iac.pipeline.step",
            "attributes": {"step_id": "template", "step_attempt": 2},
        },
    ]

    result = evaluate_assertions(records, expected_raw_content="off")

    assert any(
        item["label"] == "Repeated step attempts are distinguishable" and item["status"] == "pass" for item in result
    )


def test_step_attempt_assertion_fails_when_pipeline_step_attempt_is_missing():
    records = [
        {
            "id": "missing_attempt",
            "kind": "span",
            "name": "iac.pipeline.step",
            "attributes": {"step_id": "template"},
        }
    ]

    result = evaluate_assertions(records, expected_raw_content="off")

    attempt_presence = next(item for item in result if item["label"] == "Pipeline step_attempt is present")
    assert attempt_presence["status"] == "fail"
    assert attempt_presence["evidence_ids"] == ["missing_attempt"]
