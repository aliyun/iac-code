"""Start the real product CLI with this workflow's explicit execution policy."""

from __future__ import annotations

import os

from iac_code.services.execution_policy import install_policy

from . import execution_gate


def main() -> None:
    if not os.environ.get(execution_gate.ENV) or not os.environ.get(execution_gate.SLOT_ENV, "").isdigit():
        raise execution_gate.GateDeniedError()
    install_policy(execution_gate)
    from iac_code.cli.main import app

    app()


if __name__ == "__main__":
    main()
