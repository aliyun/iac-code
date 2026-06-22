"""Tests for iac_code.utils.log."""

from loguru import logger

from iac_code.utils.log import (
    disable_debug_at_runtime,
    enable_debug_at_runtime,
    is_debug_enabled,
    setup_logging,
)


def test_setup_logging_debug_level(tmp_path, monkeypatch):
    """Debug mode should write DEBUG-level messages to the log file."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="test123", debug=True)

    logger.debug("debug detail")
    logger.info("info message")
    logger.complete()

    log_file = tmp_path / "logs" / "test123.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "debug detail" in content
    assert "info message" in content


def test_setup_logging_info_level(tmp_path, monkeypatch):
    """Non-debug mode should only write INFO+ messages."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="test456", debug=False)

    logger.debug("should be skipped")
    logger.info("should be present")
    logger.complete()

    log_file = tmp_path / "logs" / "test456.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "should be skipped" not in content
    assert "should be present" in content


def test_setup_logging_can_mirror_to_stdout(tmp_path, monkeypatch, capsys):
    """When requested, log messages should still write the log file and mirror to stdout."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="stdout", debug=False, stdout=True)

    logger.info("visible on stdout")
    logger.complete()

    captured = capsys.readouterr()
    assert "visible on stdout" in captured.out
    assert "visible on stdout" in (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8")


def test_setup_logging_creates_latest_symlink(tmp_path, monkeypatch):
    """Should create a 'latest.log' symlink pointing to current log."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    logger.remove()

    setup_logging(session_id="sym123")

    latest = tmp_path / "logs" / "latest.log"
    assert latest.is_symlink()
    assert latest.resolve() == (tmp_path / "logs" / "sym123.log").resolve()


def test_setup_logging_uses_iac_code_log_dir(tmp_path, monkeypatch):
    """IAC_CODE_LOG_DIR should move log files out of the config directory."""
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "runtime-logs"
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("IAC_CODE_LOG_DIR", str(log_dir))
    logger.remove()

    setup_logging(session_id="custom-dir", debug=False)
    logger.info("custom log dir")
    logger.complete()

    assert (log_dir / "custom-dir.log").exists()
    assert (log_dir / "latest.log").exists()
    assert not (config_dir / "logs").exists()


def test_is_debug_enabled_reflects_setup(tmp_path, monkeypatch):
    """is_debug_enabled mirrors the debug flag passed to setup_logging."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()

    setup_logging(session_id="state1", debug=False)
    assert is_debug_enabled() is False

    setup_logging(session_id="state2", debug=True)
    assert is_debug_enabled() is True


def test_debug_env_var_is_ignored(tmp_path, monkeypatch):
    """Legacy DEBUG=1 env var must no longer enable debug logging."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.setenv("DEBUG", "1")
    logger.remove()

    setup_logging(session_id="envskip", debug=False)
    assert is_debug_enabled() is False

    logger.debug("should not appear")
    logger.info("should appear")
    logger.complete()
    content = (tmp_path / "logs" / "envskip.log").read_text(encoding="utf-8")
    assert "should not appear" not in content
    assert "should appear" in content


def test_enable_then_disable_debug_at_runtime(tmp_path, monkeypatch):
    """Runtime enable flips the flag; runtime disable flips it back and hides DEBUG."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()

    setup_logging(session_id="rt1", debug=False)
    assert is_debug_enabled() is False

    log_path = enable_debug_at_runtime("rt1")
    assert is_debug_enabled() is True
    logger.debug("after enable")
    logger.complete()
    content = log_path.read_text(encoding="utf-8")
    assert "after enable" in content

    disable_debug_at_runtime()
    assert is_debug_enabled() is False
    logger.debug("debug-post-disable")
    logger.info("info-post-disable")
    logger.complete()
    content = log_path.read_text(encoding="utf-8")
    assert "debug-post-disable" not in content
    assert "info-post-disable" in content


def test_disable_debug_drops_debug_from_startup_handler(tmp_path, monkeypatch):
    """Disabling at runtime reconfigures startup handler so DEBUG stops being recorded."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()

    setup_logging(session_id="rt2", debug=True)
    assert is_debug_enabled() is True

    disable_debug_at_runtime()
    assert is_debug_enabled() is False

    logger.debug("should not appear")
    logger.info("should appear")
    logger.complete()
    content = (tmp_path / "logs" / "rt2.log").read_text(encoding="utf-8")
    assert "should not appear" not in content
    assert "should appear" in content


def test_enable_debug_at_runtime_is_idempotent(tmp_path, monkeypatch):
    """Calling enable twice does not stack up duplicate handlers or break anything."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()

    setup_logging(session_id="rt3", debug=False)
    enable_debug_at_runtime("rt3")
    enable_debug_at_runtime("rt3")
    assert is_debug_enabled() is True

    logger.debug("once")
    logger.complete()
    content = (tmp_path / "logs" / "rt3.log").read_text(encoding="utf-8")
    # Should have exactly one "once" entry (not duplicated)
    assert content.count("once") == 1
