import sys

from iac_code.web import server as web_server
from iac_code.web.server import _render_startup_banner


def test_startup_banner_shows_clickable_url() -> None:
    banner = _render_startup_banner("http://127.0.0.1:8766")

    assert "http://127.0.0.1:8766" in banner
    # Framed on both sides so it stands out from uvicorn's log noise.
    assert banner.count("━") > 0
    lines = [line for line in banner.splitlines() if line.strip()]
    assert lines[0].strip() == lines[-1].strip()


def test_startup_banner_width_covers_longest_line() -> None:
    banner = _render_startup_banner("http://127.0.0.1:65535")
    rule_len = len(banner.splitlines()[1])
    content_lines = [line for line in banner.splitlines() if "━" not in line and line.strip()]

    assert all(len(line) <= rule_len for line in content_lines)


def test_schedule_restart_flushes_before_reexec_and_defers():
    order = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay = delay
            self.fn = fn
            self.started = False

        def start(self):
            self.started = True

    fake = {}

    def timer_factory(delay, fn):
        fake["timer"] = FakeTimer(delay, fn)
        return fake["timer"]

    timer = web_server.schedule_restart(
        delay=0.4,
        exec_fn=lambda: order.append("exec"),
        shutdown_fn=lambda: order.append("shutdown"),
        timer_factory=timer_factory,
    )

    # Deferred: nothing ran synchronously, timer armed with our delay.
    assert order == []
    assert timer.delay == 0.4
    assert timer.started is True

    # When the timer fires, telemetry flush precedes the re-exec.
    timer.fn()
    assert order == ["shutdown", "exec"]


def test_default_reexec_uses_orig_argv_but_forces_no_open(monkeypatch):
    calls = {}
    monkeypatch.setattr(web_server.os, "execv", lambda path, argv: calls.update(path=path, argv=argv))
    web_server._default_reexec()
    assert calls["path"] == sys.orig_argv[0]
    # 重放原始 argv,但强制关闭浏览器自动打开(否则重启会多弹一个新标签页)。
    assert calls["argv"][-1] == "--no-open"
    assert "--open" not in calls["argv"]


def test_reexec_argv_forces_no_open():
    # 无 open 相关 flag → 追加 --no-open。
    assert web_server._reexec_argv(["py", "iac-code", "web"]) == ["py", "iac-code", "web", "--no-open"]
    # 既有 --open → 移除并替换为 --no-open。
    assert web_server._reexec_argv(["py", "iac-code", "web", "--open"]) == ["py", "iac-code", "web", "--no-open"]
    # 既有 --no-open → 去重,仍只保留一个。
    assert web_server._reexec_argv(["py", "iac-code", "web", "--no-open"]) == ["py", "iac-code", "web", "--no-open"]
    # 其它参数(host/port 等)原样保留。
    assert web_server._reexec_argv(["py", "iac-code", "web", "--port", "8766", "--open"]) == [
        "py",
        "iac-code",
        "web",
        "--port",
        "8766",
        "--no-open",
    ]


def test_start_update_check_invokes_background_checker():
    from iac_code import __version__
    from iac_code.services.update_checker import WEB_UPDATE_CHECK_INTERVAL_SECONDS

    calls = []
    web_server._start_update_check(start_fn=lambda **kw: calls.append(kw))

    assert len(calls) == 1
    assert calls[0]["current_version"] == __version__
    # web 是长驻进程,启动即以 6h 周期透传给后台检查器。
    assert calls[0]["interval_seconds"] == WEB_UPDATE_CHECK_INTERVAL_SECONDS


def test_start_update_check_swallows_errors():
    def boom(**kwargs):
        raise RuntimeError("network down")

    # 后台检查绝不能让启动崩溃。
    web_server._start_update_check(start_fn=boom)
