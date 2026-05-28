from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks.push_notification_config_store import PushNotificationConfigStore
from a2a.server.tasks.push_notification_sender import PushNotificationEvent, PushNotificationSender
from a2a.types import TaskPushNotificationConfig
from a2a.utils.proto_utils import to_stream_response
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.push_queue import A2APushJob, A2APushQueue, LocalFileA2APushQueue
from iac_code.a2a.push_secrets import A2APushSecretKeyring
from iac_code.a2a.types import validate_protocol_id
from iac_code.utils.file_security import restrict_file_permissions, safe_replace


class InvalidPushNotificationConfigError(ValueError):
    pass


@dataclass(frozen=True)
class A2APushConfig:
    task_id: str
    callback_url: str

    def __post_init__(self) -> None:
        validate_push_callback_url(self.callback_url)


def validate_push_callback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidPushNotificationConfigError("A2A push callback URL must be http or https")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise InvalidPushNotificationConfigError("A2A push callback URL must not target private or local hosts")
    try:
        address = ip_address(host)
    except ValueError:
        return url
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise InvalidPushNotificationConfigError("A2A push callback URL must not target private or local hosts")
    return url


class A2APushConfigStore(PushNotificationConfigStore):
    def __init__(
        self,
        *,
        persistence: A2APersistenceStore,
        secret_keyring: A2APushSecretKeyring | None = None,
    ) -> None:
        self._root = Path(persistence.root) / "push_configs"
        self._secret_keyring = secret_keyring or A2APushSecretKeyring(Path(persistence.root) / "push_keys.json")
        self._root.mkdir(parents=True, exist_ok=True)
        restrict_file_permissions(self._root, directory=True)

    async def set_info(
        self,
        task_id: str,
        notification_config: TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> None:
        task_id = validate_protocol_id(task_id)
        if not notification_config.url:
            return
        validate_push_callback_url(notification_config.url)
        config = TaskPushNotificationConfig()
        config.CopyFrom(notification_config)
        if not config.id:
            config.id = task_id
        config.id = validate_protocol_id(config.id)
        config.task_id = task_id

        path = self._config_path(_owner(context), task_id, config.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        restrict_file_permissions(path.parent, directory=True)
        data = self._config_to_storage(config)
        _write_json_atomic(path, data)

    async def get_info(self, task_id: str, context: ServerCallContext) -> list[TaskPushNotificationConfig]:
        return self._load_configs_for_owner(_owner(context), validate_protocol_id(task_id))

    async def get_info_for_dispatch(self, task_id: str) -> list[TaskPushNotificationConfig]:
        task_id = validate_protocol_id(task_id)
        configs: list[TaskPushNotificationConfig] = []
        if not self._root.exists():
            return configs
        for owner_dir in self._root.iterdir():
            if owner_dir.is_dir():
                configs.extend(self._load_configs_for_owner(owner_dir.name, task_id, owner_is_hashed=True))
        return configs

    async def resolve_headers_for_dispatch(self, task_id: str, config_id: str) -> dict[str, str]:
        task_id = validate_protocol_id(task_id)
        config_id = validate_protocol_id(config_id)
        for config in await self.get_info_for_dispatch(task_id):
            if config.id == config_id:
                return _notification_headers(config)
        return {}

    async def delete_info(self, task_id: str, context: ServerCallContext, config_id: str | None = None) -> None:
        owner = _owner(context)
        task_id = validate_protocol_id(task_id)
        if config_id:
            path = self._config_path(owner, task_id, validate_protocol_id(config_id))
            path.unlink(missing_ok=True)
            return
        task_dir = self._owner_dir(owner) / task_id
        if not task_dir.exists():
            return
        for path in task_dir.glob("*.json"):
            path.unlink(missing_ok=True)

    def _load_configs_for_owner(
        self, owner: str, task_id: str, *, owner_is_hashed: bool = False
    ) -> list[TaskPushNotificationConfig]:
        owner_dir = self._root / owner if owner_is_hashed else self._owner_dir(owner)
        task_dir = owner_dir / task_id
        if not task_dir.exists():
            return []
        configs: list[TaskPushNotificationConfig] = []
        for path in sorted(task_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                config = TaskPushNotificationConfig()
                ParseDict(self._config_from_storage(data), config, ignore_unknown_fields=True)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            configs.append(config)
        return configs

    def _config_path(self, owner: str, task_id: str, config_id: str) -> Path:
        return self._owner_dir(owner) / task_id / f"{config_id}.json"

    def _owner_dir(self, owner: str) -> Path:
        return self._root / hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _config_to_storage(self, config: TaskPushNotificationConfig) -> dict[str, Any]:
        data = MessageToDict(config)
        encrypted_fields: dict[str, dict[str, str]] = {}
        token = data.pop("token", "")
        if token:
            encrypted_fields["token"] = self._secret_keyring.encrypt(str(token))
        authentication = data.get("authentication")
        if isinstance(authentication, dict):
            credentials = authentication.pop("credentials", "")
            if credentials:
                encrypted_fields["authentication.credentials"] = self._secret_keyring.encrypt(str(credentials))
        if encrypted_fields:
            data["iacCodeEncryptedFields"] = {"version": 1, "fields": encrypted_fields}
        return data

    def _config_from_storage(self, data: dict[str, Any]) -> dict[str, Any]:
        data = dict(data)
        encrypted = data.pop("iacCodeEncryptedFields", None)
        if not isinstance(encrypted, dict):
            return data
        fields = encrypted.get("fields")
        if not isinstance(fields, dict):
            return data
        token = fields.get("token")
        if isinstance(token, dict):
            data["token"] = self._secret_keyring.decrypt(token)
        credentials = fields.get("authentication.credentials")
        if isinstance(credentials, dict):
            authentication = dict(data.get("authentication") or {})
            authentication["credentials"] = self._secret_keyring.decrypt(credentials)
            data["authentication"] = authentication
        return data


class A2APushSender(PushNotificationSender):
    def __init__(
        self,
        *,
        config_store: PushNotificationConfigStore,
        queue: A2APushQueue | None = None,
        metrics: A2AMetrics | None = None,
        persistence: A2APersistenceStore | None = None,
        **_: Any,
    ) -> None:
        self._config_store = config_store
        if queue is None:
            if persistence is None:
                raise ValueError("A2APushSender requires a push queue.")
            queue = LocalFileA2APushQueue(
                Path(persistence.root) / "push_queue",
                secret_keyring=getattr(config_store, "_secret_keyring", None),
            )
        self._queue = queue
        self._metrics = metrics or NoOpA2AMetrics()

    async def send_notification(self, task_id: str, event: PushNotificationEvent) -> None:
        configs = await self._config_store.get_info_for_dispatch(validate_protocol_id(task_id))
        payload = MessageToDict(to_stream_response(event), preserving_proto_field_name=False)
        for config in configs:
            await self._queue.enqueue(
                A2APushJob(
                    task_id=task_id,
                    config_id=config.id or task_id,
                    url=validate_push_callback_url(config.url),
                    payload=payload,
                )
            )
            self._metrics.record_push_enqueued()

    async def aclose(self) -> None:
        return None


class A2APushNotifier:
    def __init__(
        self,
        *,
        persistence: A2APersistenceStore,
        http_client: Any | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        self._persistence = persistence
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient()
        self._push_dir = Path(persistence.root) / "push"
        self._max_attempts = max(1, max_attempts)
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)

    def save_config(self, config: A2APushConfig) -> None:
        self._push_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self._push_dir / f"{config.task_id}.json", asdict(config))

    def load_config(self, task_id: str) -> A2APushConfig | None:
        path = self._push_dir / f"{task_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return A2APushConfig(**data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    async def notify_task_state(self, *, task_id: str, context_id: str, state: str) -> bool:
        config = self.load_config(task_id)
        if config is None:
            return False
        payload = {"taskId": task_id, "contextId": context_id, "state": state}
        for attempt in range(self._max_attempts):
            try:
                response = await self._http_client.post(config.callback_url, json=payload, timeout=5.0)
                response.raise_for_status()
                return True
            except Exception:
                if attempt == self._max_attempts - 1:
                    raise
                if self._retry_delay_seconds:
                    await asyncio.sleep(self._retry_delay_seconds)
        return False

    async def aclose(self) -> None:
        if not self._owns_http_client:
            return
        close = getattr(self._http_client, "aclose", None)
        if close is not None:
            await close()


def _notification_headers(config: TaskPushNotificationConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.token:
        headers["X-A2A-Notification-Token"] = config.token
    if config.HasField("authentication"):
        scheme = config.authentication.scheme.lower()
        credentials = config.authentication.credentials
        if scheme == "bearer" and credentials:
            headers["Authorization"] = f"Bearer {credentials}"
        elif scheme == "basic" and credentials:
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        elif scheme and credentials:
            headers["Authorization"] = f"{config.authentication.scheme} {credentials}"
    return headers


def _owner(context: ServerCallContext) -> str:
    return resolve_user_scope(context)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        restrict_file_permissions(tmp_path, directory=False)
        safe_replace(str(tmp_path), str(path))
        restrict_file_permissions(path, directory=False)
    finally:
        tmp_path.unlink(missing_ok=True)
