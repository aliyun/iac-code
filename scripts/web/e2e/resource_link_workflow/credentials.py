"""OPS-only OAuth migration and secret-safe preparation CLI snapshots.

Web config/session roots remain independent. OAuth rotation has one canonical
YAML source; workflow CLI queries receive only a temporary STS profile, never a
second copy of the rotating refresh token. Models cannot invoke migration.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .files import WorkflowError, atomic_write, json_write, read_json
from .store import Store


def source_path(root: Path) -> Path:
    return root / "private" / "cloud-source" / ".cloud-credentials.yml"


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise WorkflowError("credential directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _check_source_paths(root: Path) -> None:
    for path in (
        root / "private",
        source_path(root).parent,
        source_path(root),
        source_path(root).parent / "source.json",
    ):
        if path.is_symlink():
            raise WorkflowError("canonical credential source must not be a symlink")
        if path.exists() and os.name != "nt" and path.stat().st_mode & 0o077:
            raise WorkflowError("canonical credential source must be private")


def source_readiness(root: Path) -> dict:
    """Current non-secret metadata, not a signed identity or an authorization.

    Never refresh, fall back to user configuration, or expose credential fields
    during preparation. Expired STS metadata means normal provider refresh is
    required at execution, not that a historical outage remains unresolved.
    """
    observed_at = time.time()
    value = {"observed_at": observed_at, "signed_identity_verified": False, "read_only": True}
    canonical = source_path(root)
    marker = canonical.parent / "source.json"
    if not canonical.exists() and not marker.exists():
        return {**value, "status": "not_adopted"}
    try:
        _check_source_paths(root)
        from iac_code.services.providers.aliyun import AliyunCredentials

        approved = read_json(marker)
        credential = AliyunCredentials._load_from_iac_code_path(canonical)
        if (
            credential is None
            or credential.mode != "OAuth"
            or credential.oauth_site_type != "CN"
            or credential.region_id != approved["region"]
        ):
            return {**value, "status": "source_unavailable"}
        complete = bool(credential.access_key_id and credential.access_key_secret and credential.sts_token)
        usable = complete and credential.sts_expiration > observed_at + 60
        return {
            **value,
            "status": "signer_current" if usable else "refresh_required",
            "approved_account": approved["account"],
            "region": credential.region_id,
            "mode": "OAuth",
            "site": "CN",
            "sts_expires_at": credential.sts_expiration,
            "sts_remaining_seconds": max(0, int(credential.sts_expiration - observed_at)),
            "refresh_performed": False,
        }
    except (OSError, ValueError, KeyError):
        # Parsing errors can contain YAML/credential data; never echo them.
        return {**value, "status": "source_unavailable"}


def adopt_oauth(store: Store, cli_config: Path, account: str, region: str) -> dict:
    """Bounded identity check, recoverable migration; never authorize cloud writes."""
    from iac_code.services.providers.aliyun import MODE_FIELDS, AliyunCredential, AliyunCredentials

    from .runtime import exclusive

    with exclusive(store.root / "cloud-source-ops.lock"):
        status = store.status()
        if not status["paused"] or status["jobs"] or any(s["web_pid"] or s["task_id"] for s in status["slots"]):
            raise WorkflowError("pause and drain all jobs; stop original Web instances before credential migration")
        canonical = source_path(store.root)
        _check_source_paths(store.root)
        aliases = [store.root / "slots" / str(s["id"]) / "config" / canonical.name for s in status["slots"]]
        marker = canonical.parent / "source.json"
        if (
            marker.exists()
            and canonical.is_file()
            and all(a.is_symlink() and a.resolve() == canonical for a in aliases)
        ):
            previous = read_json(marker)
            if previous["account"] != account or previous["region"] != region:
                raise WorkflowError("canonical source identity cannot be changed by repeating adoption")
            return {"already_adopted": True, "account": account, "region": region}
        if canonical.exists() or marker.exists() or any(a.is_symlink() or not a.is_file() for a in aliases):
            raise WorkflowError(
                "partial/existing credential migration; inspect original private backups, do not overwrite"
            )
        if os.name != "nt" and any(a.stat().st_mode & 0o077 for a in aliases):
            raise WorkflowError("original slot credentials must be private")
        if not account.isdecimal() or not region.startswith("cn-"):
            raise WorkflowError("explicit approved domestic account and region required")
        cli_config = cli_config.resolve(strict=True)
        if os.name != "nt" and cli_config.stat().st_mode & 0o077:
            raise WorkflowError("source CLI credentials must be private")
        argv = [
            "aliyun",
            "sts",
            "GetCallerIdentity",
            "--profile",
            "test-guima",
            "--config-path",
            str(cli_config),
            "--region",
            region,
            "--retry-count",
            "0",
            "--connect-timeout",
            "5",
            "--read-timeout",
            "10",
        ]
        try:
            result = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=25, check=False)
            identity = json.loads(result.stdout) if result.returncode == 0 else {}
            profiles = read_json(cli_config)["profiles"]
            matches = [p for p in profiles if p.get("name") == "test-guima"]
            profile = matches[0] if len(matches) == 1 else {}
            if identity.get("AccountId") != account or not identity.get("RequestId"):
                raise WorkflowError("signed identity did not confirm approved account; no migration")
            if (
                profile.get("mode") != "OAuth"
                or profile.get("oauth_site_type") != "CN"
                or profile.get("region_id") != region
                or int(profile.get("sts_expiration") or 0) <= time.time() + 120
                or not all(
                    profile.get(k) for k in ("access_key_id", "access_key_secret", "sts_token", "oauth_refresh_token")
                )
            ):
                raise WorkflowError("current complete domestic OAuth source required; no migration")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            raise WorkflowError("bounded credential identity/source check failed; no secrets exposed") from error
        backup = store.root / "backups" / ("oauth-adoption-" + uuid.uuid4().hex)
        _private_directory(backup)
        _private_directory(store.root / "private")
        _private_directory(canonical.parent)
        json_write(
            backup / "intent.json", {"account": account, "region": region, "state": "migration_started"}, private=True
        )
        for index, alias in enumerate(aliases):
            atomic_write(backup / f"slot-{index}.yml", alias.read_bytes(), private=True)
        fields = {name: profile[name] for name, _, _ in MODE_FIELDS["OAuth"] if name in profile}
        credential = AliyunCredential(mode="OAuth", region_id=region, **fields)
        credential.credential_source = "iac_code"
        credential.credential_source_path = str(canonical)
        AliyunCredentials.save(credential)
        # Backups precede every replacement. A crash leaves an explicit partial
        # migration that refuses another adoption, not four newly rotated copies.
        for alias in aliases:
            temporary = alias.parent / (".cloud-source-" + uuid.uuid4().hex)
            temporary.symlink_to(canonical)
            os.replace(temporary, alias)
        value = {
            "schema_version": 1,
            "account": account,
            "region": region,
            "profile": "test-guima",
            "identity": identity,
            "raw_identity_stdout": result.stdout,
            "backup": backup.relative_to(store.root).as_posix(),
            "shared_slots": len(aliases),
            "cli_policy": "temporary STS snapshot; never refresh the original OAuth CLI profile during this run",
        }
        json_write(marker, value, private=True)
        return {
            "adopted": True,
            "account": account,
            "region": region,
            "shared_slots": len(aliases),
            "evidence": marker.relative_to(store.root).as_posix(),
            "backup": backup.relative_to(store.root).as_posix(),
            "new_pass": 0,
        }


@contextmanager
def cli_snapshot(root: Path) -> Iterator[Path | None]:
    """Refresh one canonical source; transient CLI profile contains no OAuth tokens."""
    marker = source_path(root).parent / "source.json"
    _check_source_paths(root)
    if not marker.exists():
        if source_path(root).exists():
            raise WorkflowError("partial credential migration; do not fall back to the original CLI profile")
        yield None  # Legacy setup, before explicit OPS adoption only.
        return
    from iac_code.services.providers.aliyun import AliyunCredentials

    canonical = source_path(root)
    try:
        approved = read_json(marker)
        credential = AliyunCredentials._load_from_iac_code_path(canonical)
        if (
            credential is None
            or credential.mode != "OAuth"
            or credential.oauth_site_type != "CN"
            or credential.region_id != approved["region"]
        ):
            raise WorkflowError("canonical OAuth source unavailable")
        credential = AliyunCredentials.refresh_oauth_if_needed(credential)
        if credential.sts_expiration <= time.time() + 60 or not all(
            (credential.access_key_id, credential.access_key_secret, credential.sts_token)
        ):
            raise WorkflowError("canonical signing credentials unavailable")
    except Exception as error:
        raise WorkflowError("canonical credential refresh failed; do not fall back to a stale CLI clone") from error
    folder = root / "private" / "cli-snapshots"
    _private_directory(folder)
    fd, filename = tempfile.mkstemp(prefix="sts-", suffix=".json", dir=folder)
    os.close(fd)
    path = Path(filename)
    try:
        json_write(
            path,
            {
                "current": "test-guima",
                "profiles": [
                    {
                        "name": "test-guima",
                        "mode": "StsToken",
                        "region_id": credential.region_id,
                        "access_key_id": credential.access_key_id,
                        "access_key_secret": credential.access_key_secret,
                        "sts_token": credential.sts_token,
                    }
                ],
            },
            private=True,
        )
        yield path
    finally:
        path.unlink(missing_ok=True)
