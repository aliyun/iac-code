#!/usr/bin/env python3
"""Export short-lived test STS copies; OAuth rotation stays in one durable source.

run_e2e.py --cloud-credential-helper invokes this as ``cloud --output PATH``.
Set IAC_CODE_E2E_CLOUD_SOURCE to the existing test configuration directory.
No credential values or account identifiers are written to stdout/stderr.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml


def export(output: Path) -> None:
    source = Path(os.environ["IAC_CODE_E2E_CLOUD_SOURCE"]).expanduser().resolve()
    source_file = source / ".cloud-credentials.yml"
    if output.absolute() == source_file or output.resolve() == source_file.resolve():
        raise ValueError("The export must not replace the durable source")
    os.environ["IAC_CODE_CONFIG_DIR"] = str(source)
    from iac_code.services.providers.aliyun import AliyunCredentials

    credential = AliyunCredentials.refresh_oauth_if_needed(AliyunCredentials.load())
    if credential.mode not in {"OAuth", "StsToken"} or not credential.sts_token:
        raise ValueError("This helper requires short-lived test credentials")
    fields = dataclasses.asdict(credential)
    payload = {key: fields[key] for key in (
        "access_key_id", "access_key_secret", "sts_token", "sts_expiration", "region_id",
    )}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".test-sts-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump({"aliyun": {"mode": "StsToken", **payload}}, stream)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("cloud",))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        export(args.output)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
