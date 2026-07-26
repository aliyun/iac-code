from __future__ import annotations

from scripts.repl.e2e.run_contract_scenarios import _transport_actions


def test_transport_actions_reads_capture(tmp_path) -> None:
    path = tmp_path / "transport.jsonl"
    path.write_text('{"action":"DescribeVpcs"}\n', encoding="utf-8")

    assert _transport_actions(path) == ["DescribeVpcs"]
