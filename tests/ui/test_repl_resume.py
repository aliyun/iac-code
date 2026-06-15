"""Regression test for N-I1: cross-mode resume must not silently drop history."""

from pathlib import Path

from iac_code.pipeline.config import RunMode


class FakeSessionStorage:
    """Minimal storage stub that records and returns canned messages."""

    def __init__(self, messages: list[dict]):
        self._messages = list(messages)
        self._base_dir: Path | None = None
        self.loaded_cwds: list[str] = []

    def session_path(self, cwd: str, session_id: str) -> Path:
        # Each call returns a path under self._base_dir so the parent dir
        # is configurable. In tests we set self._base_dir before calls.
        assert self._base_dir is not None, "test must set _base_dir"
        return self._base_dir / session_id / "session.jsonl"

    def session_dir(self, cwd: str, session_id: str) -> Path:
        assert self._base_dir is not None, "test must set _base_dir"
        return Path(cwd) / session_id

    def exists(self, cwd: str, session_id: str) -> bool:
        return True

    def load(self, cwd: str, session_id: str) -> list[dict]:
        self.loaded_cwds.append(cwd)
        return list(self._messages)

    def repair_interrupted(self, messages: list[dict]) -> list[dict]:
        return list(messages)


def _make_repl_under_test(
    tmp_path: Path,
    messages: list[dict],
    pipeline_sidecar: bool,
    runtime_mode: RunMode,
):
    """Build an InlineREPL with the minimum state needed to test
    `_load_resume_messages` in isolation. No real services started."""
    from iac_code.ui.repl import InlineREPL

    session_id = "test-session"
    if pipeline_sidecar:
        # Sidecar lives at <session_dir>/pipeline/ after 问题 4.
        sidecar = tmp_path / session_id / "pipeline"
        sidecar.mkdir(parents=True, exist_ok=True)
        (sidecar / "meta.yaml").write_text("current_step: a\n", encoding="utf-8")

    # Bypass __init__ side effects — we only need the few attributes the
    # method touches.
    repl = InlineREPL.__new__(InlineREPL)
    storage = FakeSessionStorage(messages)
    storage._base_dir = tmp_path
    repl._session_storage = storage
    repl._original_cwd = str(tmp_path)
    repl._session_id = session_id
    repl._runtime_mode = runtime_mode
    return repl


def test_pipeline_mode_with_sidecar_returns_empty(monkeypatch, tmp_path):
    """In pipeline mode, a sidecar takes priority — messages are not loaded.
    This is the existing behavior; preserve it after the fix."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    messages = [{"role": "user", "content": "msg1"}, {"role": "assistant", "content": "reply1"}]
    repl = _make_repl_under_test(tmp_path, messages, pipeline_sidecar=True, runtime_mode=RunMode.PIPELINE)
    result = repl._load_resume_messages("test-session")
    assert result == []


def test_pipeline_mode_with_working_directory_sidecar_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline_cwd = tmp_path / "pipeline-cwd"
    original_cwd = tmp_path / "original-cwd"
    sidecar = pipeline_cwd / "test-session" / "pipeline"
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / "meta.yaml").write_text("current_step: a\n", encoding="utf-8")
    messages = [{"role": "user", "content": "msg1"}]
    repl = _make_repl_under_test(original_cwd, messages, pipeline_sidecar=False, runtime_mode=RunMode.PIPELINE)
    monkeypatch.setenv("IAC_CODE_CWD", str(pipeline_cwd))

    result = repl._load_resume_messages("test-session")

    assert result == []
    assert repl._session_storage.loaded_cwds == []


def test_pipeline_mode_with_discarded_sidecar_loads_history(monkeypatch, tmp_path):
    """Discarded sidecars are terminal metadata, not active pipeline sessions."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    messages = [{"role": "user", "content": "msg1"}]
    repl = _make_repl_under_test(tmp_path, messages, pipeline_sidecar=True, runtime_mode=RunMode.PIPELINE)
    sidecar_meta = tmp_path / "test-session" / "pipeline" / "meta.yaml"
    sidecar_meta.write_text("status: discarded\ncurrent_step: null\nstate_machine: {}\n", encoding="utf-8")

    result = repl._load_resume_messages("test-session")

    assert len(result) == 1
    assert result[0]["content"] == "msg1"


def test_normal_mode_with_sidecar_loads_history(monkeypatch, tmp_path):
    """In normal mode, the sidecar must be IGNORED and the chat history
    returned."""
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "msg2"},
    ]
    repl = _make_repl_under_test(tmp_path, messages, pipeline_sidecar=True, runtime_mode=RunMode.NORMAL)
    result = repl._load_resume_messages("test-session")
    assert len(result) == 3
    assert result[0]["content"] == "msg1"


def test_normal_mode_without_sidecar_loads_history(monkeypatch, tmp_path):
    """Sanity: normal mode with no sidecar still loads history (regression
    guard against breaking the happy path)."""
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    messages = [{"role": "user", "content": "msg1"}]
    repl = _make_repl_under_test(tmp_path, messages, pipeline_sidecar=False, runtime_mode=RunMode.NORMAL)
    result = repl._load_resume_messages("test-session")
    assert len(result) == 1
