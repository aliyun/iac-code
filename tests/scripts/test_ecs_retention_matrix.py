from scripts.ci.run_e2e import CASES as REGISTERED
from scripts.ci.run_ecs_retention_matrix import CASES, GROUPS, STABILITY_CASES


def test_thirteen_groups_use_only_real_a2a_readonly_cases():
    assert set(GROUPS) == {f"A{index:02}" for index in range(1, 14)}
    selected = [case for case in REGISTERED if case.name in CASES]
    assert len(selected) == len(CASES) == 26
    assert all(not case.cloud_write and not case.multimodal for case in selected)
    assert all(case.live_runner not in {"selling", "smoke", "repl"} for case in selected)
    assert not any(
        "mock" in case.name or "boundaries-a2a" in case.name and "context-" not in case.name for case in selected
    )
    assert STABILITY_CASES <= set(CASES)
    assert len(STABILITY_CASES) == 5
    assert (len(CASES) + 2 * len(STABILITY_CASES)) * 4 == 144
