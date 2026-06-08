import json

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import TaskPushNotificationConfig, TaskState, TaskStatus, TaskStatusUpdateEvent
from cryptography.fernet import Fernet

from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.push import (
    A2APushConfig,
    A2APushConfigStore,
    A2APushNotifier,
    A2APushSecretKeyring,
    A2APushSender,
    InvalidPushNotificationConfigError,
)
from iac_code.a2a.push_queue import LocalFileA2APushQueue
from iac_code.a2a.push_secrets import A2APushSecretError


class FakeHTTPClient:
    def __init__(self, *, failures: int = 0) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.failures = failures

    async def post(
        self,
        url: str,
        json: dict[str, object],
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        self.posts.append((url, {"json": json, "headers": headers or {}}))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("temporary push failure")

        class Response:
            def raise_for_status(self) -> None:
                return None

        return Response()

    async def aclose(self) -> None:
        self.closed = True


def test_push_config_rejects_non_http_url() -> None:
    with pytest.raises(InvalidPushNotificationConfigError):
        A2APushConfig(task_id="task-1", callback_url="file:///tmp/callback")


@pytest.mark.asyncio
async def test_notifier_posts_terminal_task_payload(tmp_path) -> None:
    http = FakeHTTPClient()
    persistence = A2APersistenceStore(tmp_path)
    notifier = A2APushNotifier(persistence=persistence, http_client=http)
    config = A2APushConfig(task_id="task-1", callback_url="https://example.test/a2a")

    notifier.save_config(config)
    delivered = await notifier.notify_task_state(task_id="task-1", context_id="ctx-1", state="completed")

    assert delivered is True
    assert http.posts[0][0] == "https://example.test/a2a"
    assert http.posts[0][1]["json"]["taskId"] == "task-1"


@pytest.mark.asyncio
async def test_notifier_retries_temporary_push_failures(tmp_path) -> None:
    http = FakeHTTPClient(failures=2)
    persistence = A2APersistenceStore(tmp_path)
    notifier = A2APushNotifier(persistence=persistence, http_client=http, retry_delay_seconds=0)
    notifier.save_config(A2APushConfig(task_id="task-1", callback_url="https://example.test/a2a"))

    delivered = await notifier.notify_task_state(task_id="task-1", context_id="ctx-1", state="completed")

    assert delivered is True
    assert len(http.posts) == 3


@pytest.mark.asyncio
async def test_notifier_does_not_close_injected_http_client(tmp_path) -> None:
    http = FakeHTTPClient()
    notifier = A2APushNotifier(persistence=A2APersistenceStore(tmp_path), http_client=http)

    await notifier.aclose()

    assert http.closed is False


@pytest.mark.asyncio
async def test_push_config_store_persists_configs_by_owner(tmp_path) -> None:
    store = A2APushConfigStore(persistence=A2APersistenceStore(tmp_path))
    alice = ServerCallContext()
    alice.user = type("User", (), {"user_name": "alice", "is_authenticated": True})()
    bob = ServerCallContext()
    bob.user = type("User", (), {"user_name": "bob", "is_authenticated": True})()

    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(task_id="task-1", id="cfg-1", url="https://callback.example/a2a"),
        alice,
    )

    assert [config.id for config in await store.get_info("task-1", alice)] == ["cfg-1"]
    assert await store.get_info("task-1", bob) == []
    assert [config.id for config in await store.get_info_for_dispatch("task-1")] == ["cfg-1"]


@pytest.mark.asyncio
async def test_push_config_store_preserves_existing_config_when_atomic_replace_fails(monkeypatch, tmp_path) -> None:
    store = A2APushConfigStore(persistence=A2APersistenceStore(tmp_path))
    context = ServerCallContext()
    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(task_id="task-1", id="cfg-1", url="https://old.example/a2a"),
        context,
    )

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("iac_code.a2a.push.safe_replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        await store.set_info(
            "task-1",
            TaskPushNotificationConfig(task_id="task-1", id="cfg-1", url="https://new.example/a2a"),
            context,
        )

    configs = await store.get_info("task-1", context)
    assert configs[0].url == "https://old.example/a2a"
    assert list((tmp_path / "push_configs").glob("**/*.tmp")) == []


def test_push_notifier_preserves_existing_config_when_atomic_replace_fails(monkeypatch, tmp_path) -> None:
    notifier = A2APushNotifier(persistence=A2APersistenceStore(tmp_path), http_client=FakeHTTPClient())
    notifier.save_config(A2APushConfig(task_id="task-1", callback_url="https://old.example/a2a"))

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("iac_code.a2a.push.safe_replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        notifier.save_config(A2APushConfig(task_id="task-1", callback_url="https://new.example/a2a"))

    assert notifier.load_config("task-1").callback_url == "https://old.example/a2a"
    assert list((tmp_path / "push").glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_push_config_store_encrypts_token_and_auth_credentials_at_rest(tmp_path) -> None:
    store = A2APushConfigStore(
        persistence=A2APersistenceStore(tmp_path),
        secret_keyring=A2APushSecretKeyring(tmp_path / "push_keys.json"),
    )
    context = ServerCallContext()

    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(
            task_id="task-1",
            id="cfg-1",
            url="https://callback.example/a2a",
            token="token-1",
            authentication={"scheme": "bearer", "credentials": "secret-1"},
        ),
        context,
    )

    raw = next((tmp_path / "push_configs").glob("*/*/cfg-1.json")).read_text(encoding="utf-8")
    assert "token-1" not in raw
    assert "secret-1" not in raw
    assert "iacCodeEncryptedFields" in raw

    loaded = await store.get_info("task-1", context)
    assert loaded[0].token == "token-1"
    assert loaded[0].authentication.credentials == "secret-1"
    assert await store.resolve_headers_for_dispatch("task-1", "cfg-1") == {
        "X-A2A-Notification-Token": "token-1",
        "Authorization": "Bearer secret-1",
    }


@pytest.mark.asyncio
async def test_push_config_store_key_rotation_keeps_old_configs_readable(tmp_path) -> None:
    keyring = A2APushSecretKeyring(tmp_path / "push_keys.json")
    store = A2APushConfigStore(persistence=A2APersistenceStore(tmp_path), secret_keyring=keyring)
    context = ServerCallContext()

    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(task_id="task-1", id="cfg-old", url="https://callback.example/a2a", token="old"),
        context,
    )
    old_key_id = keyring.active_key_id

    new_key_id = keyring.rotate()
    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(task_id="task-1", id="cfg-new", url="https://callback.example/a2a", token="new"),
        context,
    )

    assert new_key_id != old_key_id
    configs = {config.id: config.token for config in await store.get_info("task-1", context)}
    assert configs == {"cfg-old": "old", "cfg-new": "new"}
    raw_old = next((tmp_path / "push_configs").glob("*/*/cfg-old.json")).read_text(encoding="utf-8")
    raw_new = next((tmp_path / "push_configs").glob("*/*/cfg-new.json")).read_text(encoding="utf-8")
    assert old_key_id in raw_old
    assert new_key_id in raw_new


def test_push_secret_keyring_preserves_created_at_on_rotate(tmp_path) -> None:
    keyring_path = tmp_path / "keys.json"
    keyring = A2APushSecretKeyring(keyring_path)

    # Trigger initial key creation
    _ = keyring.active_key_id
    first_key_id = keyring.active_key_id

    # Read persisted data and verify initial createdAt
    raw = json.loads(keyring_path.read_text(encoding="utf-8"))
    original_created_at = raw["keys"][0]["createdAt"]
    assert original_created_at > 0

    # Rotate to add a new key - should NOT overwrite existing createdAt
    new_key_id = keyring.rotate()

    raw_after = json.loads(keyring_path.read_text(encoding="utf-8"))
    keys_by_id = {k["id"]: k for k in raw_after["keys"]}
    assert keys_by_id[first_key_id]["createdAt"] == original_created_at
    assert keys_by_id[new_key_id]["createdAt"] >= original_created_at


def test_push_secret_keyring_preserves_created_at_across_reload(tmp_path) -> None:
    keyring_path = tmp_path / "keys.json"
    keyring = A2APushSecretKeyring(keyring_path)

    # Trigger initial key creation
    _ = keyring.active_key_id
    first_key_id = keyring.active_key_id

    raw = json.loads(keyring_path.read_text(encoding="utf-8"))
    original_created_at = raw["keys"][0]["createdAt"]

    # Load a fresh keyring from disk, rotate, and verify createdAt is preserved
    keyring2 = A2APushSecretKeyring(keyring_path)
    keyring2.rotate()

    raw_after = json.loads(keyring_path.read_text(encoding="utf-8"))
    keys_by_id = {k["id"]: k for k in raw_after["keys"]}
    assert keys_by_id[first_key_id]["createdAt"] == original_created_at


def test_push_secret_keyring_can_use_env_managed_keys(monkeypatch, tmp_path) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(
        "IAC_CODE_A2A_PUSH_KEYRING",
        json.dumps({"activeKeyId": "shared", "keys": [{"id": "shared", "fernetKey": key}]}),
    )
    producer = A2APushSecretKeyring(tmp_path / "producer.json")
    consumer = A2APushSecretKeyring(tmp_path / "consumer.json")

    envelope = producer.encrypt("shared secret")

    assert consumer.decrypt(envelope) == "shared secret"
    assert producer.active_key_id == "shared"
    assert not (tmp_path / "producer.json").exists()
    with pytest.raises(A2APushSecretError, match="environment-managed"):
        producer.rotate()


@pytest.mark.asyncio
async def test_push_sender_enqueues_standard_stream_response_without_persisting_auth_headers(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    store = A2APushConfigStore(persistence=persistence)
    queue = LocalFileA2APushQueue(tmp_path / "push_queue")
    sender = A2APushSender(config_store=store, queue=queue)
    context = ServerCallContext()
    await store.set_info(
        "task-1",
        TaskPushNotificationConfig(
            task_id="task-1",
            id="cfg-1",
            url="https://callback.example/a2a",
            token="token-1",
            authentication={"scheme": "bearer", "credentials": "secret"},
        ),
        context,
    )

    await sender.send_notification(
        "task-1",
        TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        ),
    )

    jobs = list((tmp_path / "push_queue" / "pending").glob("*.json"))
    assert jobs
    claimed = await queue.claim()
    assert claimed is not None
    assert claimed.task_id == "task-1"
    assert claimed.config_id == "cfg-1"
    assert claimed.url == "https://callback.example/a2a"
    assert claimed.payload["statusUpdate"]["taskId"] == "task-1"
    assert claimed.headers == {}
    assert await store.resolve_headers_for_dispatch("task-1", "cfg-1") == {
        "X-A2A-Notification-Token": "token-1",
        "Authorization": "Bearer secret",
    }
