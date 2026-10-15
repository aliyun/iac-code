import ctypes
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from iac_code.a2a.execution_owner import ExecutionOwnerIdentity


class ProcFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        boot = root / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("00000000-0000-0000-0000-000000000001\n", encoding="ascii")
        self.namespace = root / "namespace"
        self.namespace.touch()
        self.write_process(1, 10)
        self.write_process(18, 200)
        (root / "self/ns").mkdir(parents=True)
        (root / "self/ns/pid").hardlink_to(self.namespace)
        (root / "self/status").write_text("Name:\tworker\nNSpid:\t18\n", encoding="ascii")

    def write_process(self, pid: int, start: int) -> None:
        process = self.root / str(pid)
        (process / "ns").mkdir(parents=True, exist_ok=True)
        namespace = process / "ns/pid"
        if not namespace.exists():
            namespace.hardlink_to(self.namespace)
        # Linux stat field 2 may itself contain spaces and parentheses.
        fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 3
        (process / "stat").write_text(f"{pid} (worker ) (pool) {' '.join(fields)}\n", encoding="ascii")


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProcFixture:
    fixture = ProcFixture(tmp_path / "proc")
    monkeypatch.setattr("iac_code.a2a.execution_owner.sys.platform", "linux")
    monkeypatch.setattr("iac_code.a2a.execution_owner.os.getpid", lambda: 18)
    monkeypatch.setattr(ExecutionOwnerIdentity, "_proc_root", fixture.root)
    return fixture


def test_capture_round_trips_kernel_process_identity(proc: ProcFixture) -> None:
    owner = ExecutionOwnerIdentity.capture()

    assert owner.pid == 18
    assert owner.runtime_incarnation == "linux-boot:00000000-0000-0000-0000-000000000001"
    assert owner.pid_namespace
    assert owner.process_start_identity == "200"
    restored = ExecutionOwnerIdentity.model_validate(owner.as_document())
    assert restored == owner
    assert restored.same_process_domain(owner)
    with pytest.raises(ValidationError):
        owner.pid = 19


@pytest.mark.parametrize("alive", [True, False])
def test_current_process_cannot_be_declared_dead(proc: ProcFixture, alive: bool) -> None:
    owner = ExecutionOwnerIdentity.capture()

    assert not owner.can_prove_dead(owner, pid_alive=lambda _: alive)


def test_live_worker_in_same_runtime_is_not_reclaimable(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    current = ExecutionOwnerIdentity.capture()
    previous = ExecutionOwnerIdentity.capture(19)

    assert current.same_process_domain(previous)
    assert not current.can_prove_dead(previous, pid_alive=lambda _: True)


def test_missing_process_in_same_domain_can_be_proved_dead(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    current = ExecutionOwnerIdentity.capture()
    previous = ExecutionOwnerIdentity.capture(19)
    (proc.root / "19/stat").unlink()

    assert current.can_prove_dead(previous, pid_alive=lambda _: False)


def test_reused_pid_is_distinguished_by_process_start(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    current = ExecutionOwnerIdentity.capture()
    previous = ExecutionOwnerIdentity.capture(19)
    proc.write_process(19, 300)

    assert current.can_prove_dead(previous, pid_alive=lambda _: True)


@pytest.mark.parametrize("field", ["runtime_incarnation", "pid_namespace", "process_start_identity"])
def test_incomplete_identity_never_proves_death(proc: ProcFixture, field: str) -> None:
    current = ExecutionOwnerIdentity.capture()
    previous = current.model_copy(update={field: None, "pid": 19})

    assert not current.can_prove_dead(previous, pid_alive=lambda _: False)
    assert not previous.can_prove_dead(current, pid_alive=lambda _: False)


@pytest.mark.parametrize("field", ["runtime_incarnation", "pid_namespace"])
def test_foreign_domain_never_uses_local_pid_probe(proc: ProcFixture, field: str) -> None:
    current = ExecutionOwnerIdentity.capture()
    previous = current.model_copy(update={field: "foreign", "pid": 19})
    probe = Mock(side_effect=AssertionError("A foreign PID must never be probed locally"))

    assert not current.same_process_domain(previous)
    assert not current.can_prove_dead(previous, pid_alive=probe)


def test_namespace_incarnation_includes_init_process_start(proc: ProcFixture) -> None:
    previous = ExecutionOwnerIdentity.capture()
    proc.write_process(1, 11)
    current = ExecutionOwnerIdentity.capture()

    assert not current.same_process_domain(previous)
    assert not current.can_prove_dead(previous, pid_alive=lambda _: False)


def test_unreadable_start_identity_with_live_pid_is_not_death(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    previous = ExecutionOwnerIdentity.capture(19)
    current = ExecutionOwnerIdentity.capture()
    (proc.root / "19/stat").write_text("malformed stat", encoding="ascii")

    assert not current.can_prove_dead(previous, pid_alive=lambda _: True)


def test_probe_error_is_inconclusive(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    previous = ExecutionOwnerIdentity.capture(19)
    current = ExecutionOwnerIdentity.capture()
    probe = Mock(side_effect=PermissionError("not permitted"))

    assert not current.can_prove_dead(previous, pid_alive=probe)


@pytest.mark.parametrize("platform", ["unsupported"])
def test_unsupported_platform_is_conservative(
    proc: ProcFixture, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr("iac_code.a2a.execution_owner.sys.platform", platform)
    owner = ExecutionOwnerIdentity.capture()

    assert owner.pid == 18
    assert not owner.same_process_domain(owner)
    assert not owner.can_prove_dead(owner.model_copy(update={"pid": 19}), pid_alive=lambda _: False)


@pytest.mark.parametrize("missing", ["sys/kernel/random/boot_id", "1/stat", "self/ns/pid", "self/status"])
def test_missing_kernel_domain_is_conservative(proc: ProcFixture, missing: str) -> None:
    (proc.root / missing).unlink()
    owner = ExecutionOwnerIdentity.capture()

    assert not owner.same_process_domain(owner)
    assert not owner.can_prove_dead(owner.model_copy(update={"pid": 19}), pid_alive=lambda _: False)


def test_foreign_proc_mount_cannot_supply_local_process_domain(proc: ProcFixture) -> None:
    (proc.root / "self/status").write_text("NSpid:\t1000\t18\n", encoding="ascii")
    owner = ExecutionOwnerIdentity.capture()

    assert not owner.same_process_domain(owner)


@pytest.mark.parametrize("status", ["Name:\tworker\n", "NSpid:\t18\nNSpid:\t18\n", "NSpid:\t19\n", "NSpid:\n"])
def test_ambiguous_proc_coordinates_cannot_supply_process_domain(proc: ProcFixture, status: str) -> None:
    (proc.root / "self/status").write_text(status, encoding="ascii")
    owner = ExecutionOwnerIdentity.capture()

    assert not owner.same_process_domain(owner)


def test_foreign_target_namespace_cannot_supply_local_process_domain(proc: ProcFixture) -> None:
    proc.write_process(19, 250)
    namespace = proc.root / "19/ns/pid"
    namespace.unlink()
    namespace.touch()
    owner = ExecutionOwnerIdentity.capture(19)

    assert not owner.same_process_domain(owner)


def test_unprivileged_worker_can_prove_dead_owner_without_reading_init_namespace(
    proc: ProcFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc.write_process(19, 250)
    original_stat = Path.stat

    def nonroot_stat(path: Path, *args, **kwargs):
        if path == proc.root / "1/ns/pid":
            raise PermissionError("root-owned init namespace is ptrace protected")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", nonroot_stat)
    previous = ExecutionOwnerIdentity.capture(19)
    current = ExecutionOwnerIdentity.capture()

    assert current.same_process_domain(previous)
    assert current.can_prove_dead(previous, pid_alive=lambda _: False)


@pytest.mark.parametrize("pid", [0, -1, True])
def test_invalid_pid_is_rejected(proc: ProcFixture, pid: int) -> None:
    with pytest.raises(ValidationError):
        ExecutionOwnerIdentity.capture(pid)


@pytest.mark.parametrize("start", ["invalid", "-1", "0200"])
def test_corrupt_start_identity_cannot_be_interpreted_as_pid_reuse(proc: ProcFixture, start: str) -> None:
    document = ExecutionOwnerIdentity.capture().as_document()
    document["processStartIdentity"] = start

    with pytest.raises(ValidationError):
        ExecutionOwnerIdentity.model_validate(document)


class DarwinNativeFixture:
    def __init__(self) -> None:
        self.boot = b"00000000-0000-0000-0000-000000000002\x00"
        self.starts = {18: (100, 200), 19: (100, 300)}
        self.proc_size_adjustment = 0
        self.reported_pid: int | None = None
        self.libc = Mock()
        self.libc.sysctlbyname.side_effect = self.sysctlbyname
        self.libproc = Mock()
        self.libproc.proc_pidinfo.side_effect = self.proc_pidinfo

    def sysctlbyname(self, name, buffer, size, new, new_size) -> int:
        assert name == b"kern.bootsessionuuid"
        assert new is None and new_size == 0
        ctypes.memmove(buffer, self.boot, len(self.boot))
        size._obj.value = len(self.boot)
        return 0

    def proc_pidinfo(self, pid, flavor, arg, buffer, size) -> int:
        assert flavor == 3 and arg == 0
        if pid not in self.starts:
            return 0
        info = buffer._obj
        info.pbi_pid = pid if self.reported_pid is None else self.reported_pid
        info.pbi_start_tvsec, info.pbi_start_tvusec = self.starts[pid]
        return size + self.proc_size_adjustment

    def load(self, name, **kwargs):
        return self.libproc if name == "/usr/lib/libproc.dylib" else self.libc


@pytest.fixture
def darwin_native(monkeypatch: pytest.MonkeyPatch) -> DarwinNativeFixture:
    native = DarwinNativeFixture()
    monkeypatch.setattr("iac_code.a2a.execution_owner.sys.platform", "darwin")
    monkeypatch.setattr("iac_code.a2a.execution_owner.os.getpid", lambda: 18)
    monkeypatch.setattr(ctypes, "CDLL", native.load)
    return native


def test_darwin_captures_native_boot_and_process_identity(darwin_native: DarwinNativeFixture) -> None:
    owner = ExecutionOwnerIdentity.capture()

    assert owner.runtime_incarnation == "darwin-boot:00000000-0000-0000-0000-000000000002"
    assert owner.pid_namespace == "darwin-global"
    assert owner.process_start_identity == "100000200"
    assert ExecutionOwnerIdentity.model_validate(owner.as_document()) == owner


def test_darwin_native_identity_distinguishes_alive_dead_and_reused_pid(
    darwin_native: DarwinNativeFixture,
) -> None:
    owner = ExecutionOwnerIdentity.capture()
    previous = ExecutionOwnerIdentity.capture(19)
    assert owner.same_process_domain(previous)
    assert not owner.can_prove_dead(previous, lambda _: True)
    assert owner.can_prove_dead(previous, lambda _: False)
    darwin_native.starts[19] = (100, 301)
    assert owner.can_prove_dead(previous, lambda _: True)
    darwin_native.boot = b"00000000-0000-0000-0000-000000000003\x00"
    assert not ExecutionOwnerIdentity.capture().can_prove_dead(previous, lambda _: False)


@pytest.mark.parametrize("failure", ["sysctl", "boot", "proc", "short", "pid", "microseconds"])
def test_darwin_native_failure_never_supplies_partial_domain(darwin_native: DarwinNativeFixture, failure: str) -> None:
    if failure == "sysctl":
        darwin_native.libc.sysctlbyname.side_effect = OSError("denied")
    elif failure == "boot":
        darwin_native.boot = b"not-a-boot-uuid\x00"
    elif failure == "proc":
        darwin_native.starts.clear()
    elif failure == "short":
        darwin_native.proc_size_adjustment = -1
    elif failure == "pid":
        darwin_native.reported_pid = 19
    else:
        darwin_native.starts[18] = (100, 1000000)
    owner = ExecutionOwnerIdentity.capture()
    assert not owner.same_process_domain(owner)
    assert owner.process_start_identity is None


class WindowsNativeFixture:
    def __init__(self) -> None:
        self.boot = 133000000000000000
        self.starts = {18: 133000000000000100, 19: 133000000000000200}
        self.silo = 0
        self.ntdll = Mock()
        self.ntdll.RtlGetCurrentServiceSessionId.side_effect = lambda: self.silo
        self.ntdll.NtQuerySystemInformation.side_effect = self.query_system
        self.kernel = Mock()
        self.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid if pid in self.starts else 0
        self.kernel.GetProcessTimes.side_effect = self.get_times
        self.kernel.CloseHandle.return_value = 1

    def query_system(self, info_class, buffer, length, returned) -> int:
        assert info_class == 3 and length == 48
        ctypes.memmove(buffer, self.boot.to_bytes(8, "little", signed=True), 8)
        returned._obj.value = 48
        return 0

    def get_times(self, handle, created, exited, kernel, user) -> int:
        created._obj.low = self.starts[handle] & 0xFFFFFFFF
        created._obj.high = self.starts[handle] >> 32
        return 1

    def load(self, name, **kwargs):
        return self.ntdll if name == "ntdll" else self.kernel


@pytest.fixture
def windows_native(monkeypatch: pytest.MonkeyPatch) -> WindowsNativeFixture:
    native = WindowsNativeFixture()
    monkeypatch.setattr("iac_code.a2a.execution_owner.sys.platform", "win32")
    monkeypatch.setattr("iac_code.a2a.execution_owner.os.getpid", lambda: 18)
    monkeypatch.setattr(ctypes, "WinDLL", native.load, raising=False)
    return native


def test_windows_captures_native_boot_and_creation_time(windows_native: WindowsNativeFixture) -> None:
    owner = ExecutionOwnerIdentity.capture()
    assert owner.runtime_incarnation == "windows-boot:133000000000000000"
    assert owner.pid_namespace == "windows-host"
    assert owner.process_start_identity == "133000000000000100"
    assert ExecutionOwnerIdentity.model_validate(owner.as_document()) == owner
    windows_native.kernel.CloseHandle.assert_called_once_with(18)


def test_windows_native_identity_distinguishes_alive_dead_and_reused_pid(
    windows_native: WindowsNativeFixture,
) -> None:
    current = ExecutionOwnerIdentity.capture()
    previous = ExecutionOwnerIdentity.capture(19)
    assert current.same_process_domain(previous)
    assert not current.can_prove_dead(previous, lambda _: True)
    assert current.can_prove_dead(previous, lambda _: False)
    windows_native.starts[19] += 1
    assert current.can_prove_dead(previous, lambda _: True)
    windows_native.boot += 1
    assert not ExecutionOwnerIdentity.capture().can_prove_dead(previous, lambda _: False)


@pytest.mark.parametrize("silo", [1, 0xFFFFFFFF])
def test_windows_silo_domain_is_unproven(windows_native: WindowsNativeFixture, silo: int) -> None:
    windows_native.silo = silo
    owner = ExecutionOwnerIdentity.capture()
    assert not owner.same_process_domain(owner)
    windows_native.kernel.OpenProcess.assert_not_called()


@pytest.mark.parametrize("failure", ["boot", "boot_length", "boot_zero", "open", "times", "start_zero", "api"])
def test_windows_native_failure_is_conservative(windows_native: WindowsNativeFixture, failure: str) -> None:
    if failure == "boot":
        windows_native.ntdll.NtQuerySystemInformation.side_effect = None
        windows_native.ntdll.NtQuerySystemInformation.return_value = -1
    elif failure == "boot_length":
        windows_native.ntdll.NtQuerySystemInformation.side_effect = lambda *args: 0
    elif failure == "boot_zero":
        windows_native.boot = 0
    elif failure == "open":
        windows_native.kernel.OpenProcess.side_effect = lambda *args: 0
    elif failure == "times":
        windows_native.kernel.GetProcessTimes.side_effect = None
        windows_native.kernel.GetProcessTimes.return_value = 0
    elif failure == "start_zero":
        windows_native.starts[18] = 0
    else:
        windows_native.ntdll.RtlGetCurrentServiceSessionId.side_effect = AttributeError("unavailable")
    owner = ExecutionOwnerIdentity.capture()
    assert not owner.same_process_domain(owner)
    assert owner.process_start_identity is None
    if failure in {"times", "start_zero"}:
        windows_native.kernel.CloseHandle.assert_called_once_with(18)
