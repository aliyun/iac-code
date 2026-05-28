"""Global test configuration."""

import os

import pytest

# Force English locale for tests so assertions match English strings
os.environ["LANGUAGE"] = "en"
os.environ["LC_ALL"] = "en_US.UTF-8"
os.environ["LANG"] = "en_US.UTF-8"

# Disable Rich/Click ANSI color output so substring assertions on help text
# (e.g. "--config" in result.stdout) work in CI where a TTY-like environment
# may otherwise insert escape sequences mid-token.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"

# Re-initialize i18n with English locale
from iac_code.i18n import setup_i18n  # noqa: E402

setup_i18n()

# Eagerly import modules that do `from iac_code.commands.registry import PromptCommand`
# at module level. If these are imported later (e.g. inside a test that has
# monkey-patched `iac_code.commands.registry.PromptCommand`), the local binding
# captures the fake class and cannot be reverted, leaking across tests.
import iac_code.skills.discovery  # noqa: E402, F401
import iac_code.skills.listing  # noqa: E402, F401
import iac_code.skills.skill_tool  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _isolate_iac_home(tmp_path_factory, monkeypatch):
    """Redirect HOME to a per-test tmp dir so tests can never write to the
    real ~/.iac-code/ (settings.yml, .credentials.yml, telemetry userID, etc.)
    even when an individual test forgets to patch a path helper.

    A separate tmp dir (not the test's own ``tmp_path``) is used so tests that
    treat ``tmp_path`` as "outside $HOME" still behave correctly.

    Also unset IAC_CODE_CONFIG_DIR so a developer's local override cannot
    leak into tests that rely on the ``Path.home() / ".iac-code"`` fallback.
    """
    fake_home = tmp_path_factory.mktemp("iac_home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("IAC_CODE_CONFIG_DIR", raising=False)
