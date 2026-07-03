use iac_code_a2a::task_store::{A2ATaskStore, Artifact, ListTasksRequest, SdkTask, TaskStoreError};
use iac_code_a2a::types::{validate_protocol_id, A2A_ID_MAX_LENGTH, TASK_STATE_SUBMITTED};

#[test]
fn validate_protocol_id_accepts_python_safe_values() {
    for value in ["abc", "abc-123", "abc_123", "abc.123", "abc:123"] {
        assert_eq!(validate_protocol_id(value), Ok(value.to_owned()));
    }
}

#[test]
fn validate_protocol_id_rejects_unsafe_values() {
    for value in [
        "",
        "space value",
        "../x",
        "x/y",
        &"x".repeat(A2A_ID_MAX_LENGTH + 1),
    ] {
        assert_eq!(
            validate_protocol_id(value),
            Err(TaskStoreError::InvalidA2AId)
        );
    }
}

#[test]
fn context_reuses_session_until_evicted_and_rejects_workspace_change() {
    let mut store = A2ATaskStore::new();
    let first = store
        .get_or_create_context("ctx-1", "/tmp/one")
        .expect("context");
    let session_id = first.session_id.clone();

    let again = store
        .get_or_create_context("ctx-1", "/tmp/one")
        .expect("context");
    assert_eq!(again.session_id, session_id);

    let error = store
        .get_or_create_context("ctx-1", "/tmp/two")
        .expect_err("workspace mismatch");
    assert_eq!(
        error,
        TaskStoreError::InvalidState("A2A context belongs to a different workspace".into())
    );
}

#[test]
fn task_id_cannot_move_between_contexts_and_expired_task_rejects_follow_up() {
    let mut store = A2ATaskStore::new().with_idle_timeout_seconds(0.0);
    store
        .get_or_create_context("ctx-1", "/tmp")
        .expect("context");
    store
        .get_or_create_task(Some("task-1"), "ctx-1")
        .expect("task");

    let mismatch = store
        .get_or_create_task(Some("task-1"), "ctx-2")
        .expect_err("context mismatch");
    assert_eq!(
        mismatch,
        TaskStoreError::InvalidState("Task belongs to a different context".into())
    );

    store.cleanup_once(1.0);
    assert_eq!(
        store.ensure_task_not_expired("task-1"),
        Err(TaskStoreError::InvalidState("A2A task expired".into()))
    );
}

#[test]
fn cleanup_keeps_sdk_task_during_tombstone_window_then_removes_it() {
    let mut store = A2ATaskStore::new()
        .with_idle_timeout_seconds(0.0)
        .with_cleanup_interval_seconds(300.0);
    store
        .get_or_create_context("ctx-1", "/tmp")
        .expect("context");
    store
        .get_or_create_task(Some("task-1"), "ctx-1")
        .expect("task");
    store.save_sdk_task(SdkTask::new("task-1", "ctx-1", TASK_STATE_SUBMITTED, 1), "");

    store.cleanup_once(1.0);
    assert!(store.get_sdk_task("task-1", "").is_some());

    store.cleanup_once(302.0);
    assert!(store.get_sdk_task("task-1", "").is_none());
}

#[test]
fn cancel_active_task_and_active_status_are_deterministic() {
    let mut store = A2ATaskStore::new();
    store
        .get_or_create_task(Some("task-1"), "ctx-1")
        .expect("task");
    assert!(!store.is_task_active("task-1"));

    store.set_task_active("task-1", true).expect("mark active");
    assert!(store.is_task_active("task-1"));
    assert!(store.cancel_task("task-1"));
    assert!(!store.is_task_active("task-1"));
    assert!(!store.cancel_task("task-1"));
}

#[test]
fn list_filters_status_sorts_desc_and_paginates_with_cursor() {
    let mut store = A2ATaskStore::new();
    store.save_sdk_task(
        SdkTask::new("task-old", "ctx-1", "TASK_STATE_WORKING", 10),
        "",
    );
    store.save_sdk_task(
        SdkTask::new("task-new", "ctx-1", "TASK_STATE_WORKING", 30),
        "",
    );
    store.save_sdk_task(
        SdkTask::new("task-failed", "ctx-1", "TASK_STATE_FAILED", 40),
        "",
    );
    store.save_sdk_task(
        SdkTask::new("task-mid", "ctx-1", "TASK_STATE_WORKING", 20),
        "",
    );

    let first = store
        .list_sdk_tasks(
            ListTasksRequest {
                status: Some("TASK_STATE_WORKING".into()),
                page_size: Some(2),
                ..Default::default()
            },
            "",
        )
        .expect("first page");

    assert_eq!(task_ids(&first.tasks), vec!["task-new", "task-mid"]);
    assert_eq!(first.page_size, 2);
    assert_eq!(first.total_size, 3);
    assert!(!first.next_page_token.is_empty());

    let second = store
        .list_sdk_tasks(
            ListTasksRequest {
                status: Some("TASK_STATE_WORKING".into()),
                page_size: Some(2),
                page_token: Some(first.next_page_token),
                ..Default::default()
            },
            "",
        )
        .expect("second page");

    assert_eq!(task_ids(&second.tasks), vec!["task-old"]);
    assert_eq!(second.next_page_token, "");
}

#[test]
fn list_rejects_invalid_page_token() {
    let mut store = A2ATaskStore::new();
    store.save_sdk_task(SdkTask::new("task-1", "ctx-1", TASK_STATE_SUBMITTED, 1), "");

    let error = store
        .list_sdk_tasks(
            ListTasksRequest {
                page_token: Some("bWlzc2luZw==".into()),
                ..Default::default()
            },
            "",
        )
        .expect_err("invalid page token");
    assert_eq!(
        error,
        TaskStoreError::InvalidParams("Invalid page token: bWlzc2luZw==".into())
    );
}

#[test]
fn list_filters_by_context_and_projects_artifacts() {
    let mut store = A2ATaskStore::new();
    let mut task_a = SdkTask::new("task-1", "ctx-a", TASK_STATE_SUBMITTED, 1);
    task_a.artifacts.push(Artifact {
        artifact_id: "artifact-task-1".into(),
        filename: "template.yaml".into(),
        media_type: "application/yaml".into(),
        byte_size: 10,
        sha256: "abc123".into(),
        uri: "file:///tmp/template.yaml".into(),
    });
    store.save_sdk_task(task_a, "");
    store.save_sdk_task(SdkTask::new("task-2", "ctx-b", TASK_STATE_SUBMITTED, 2), "");

    let without_artifacts = store
        .list_sdk_tasks(
            ListTasksRequest {
                context_id: Some("ctx-a".into()),
                ..Default::default()
            },
            "",
        )
        .expect("list");
    assert_eq!(task_ids(&without_artifacts.tasks), vec!["task-1"]);
    assert!(without_artifacts.tasks[0].artifacts.is_empty());
    assert_eq!(
        store
            .get_sdk_task("task-1", "")
            .expect("stored task")
            .artifacts[0]
            .artifact_id,
        "artifact-task-1"
    );

    let with_artifacts = store
        .list_sdk_tasks(
            ListTasksRequest {
                context_id: Some("ctx-a".into()),
                include_artifacts: true,
                ..Default::default()
            },
            "",
        )
        .expect("list");
    assert_eq!(
        with_artifacts.tasks[0].artifacts[0].artifact_id,
        "artifact-task-1"
    );
}

#[test]
fn task_store_scopes_sdk_tasks_by_owner() {
    let mut store = A2ATaskStore::new();
    store.save_sdk_task(
        SdkTask::new("alice-task", "ctx-a", TASK_STATE_SUBMITTED, 1),
        "alice",
    );
    store.save_sdk_task(
        SdkTask::new("bob-task", "ctx-b", TASK_STATE_SUBMITTED, 2),
        "bob",
    );

    let alice = store
        .list_sdk_tasks(ListTasksRequest::default(), "alice")
        .expect("alice tasks");
    let bob = store
        .list_sdk_tasks(ListTasksRequest::default(), "bob")
        .expect("bob tasks");

    assert_eq!(task_ids(&alice.tasks), vec!["alice-task"]);
    assert_eq!(task_ids(&bob.tasks), vec!["bob-task"]);
    assert!(store.get_sdk_task("bob-task", "alice").is_none());
}

fn task_ids(tasks: &[SdkTask]) -> Vec<&str> {
    tasks.iter().map(|task| task.id.as_str()).collect()
}
