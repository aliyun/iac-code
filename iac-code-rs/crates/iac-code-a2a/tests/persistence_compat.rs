use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_a2a::persistence::{
    A2AContextSnapshot, A2APersistenceStore, A2ARouteSnapshot, A2ATaskSnapshot,
};
use iac_code_a2a::types::TaskStoreError;

#[test]
fn persistence_round_trips_task_and_context_snapshots() {
    let root = temp_dir("round-trip");
    let store = A2APersistenceStore::new(&root);

    store
        .save_task(A2ATaskSnapshot {
            task_id: "task-1".to_owned(),
            context_id: "ctx-1".to_owned(),
            state: "working".to_owned(),
            output_text: vec!["hi".to_owned()],
            status_message: String::new(),
            updated_at: 12.5,
        })
        .expect("save task");
    store
        .save_context(A2AContextSnapshot {
            context_id: "ctx-1".to_owned(),
            session_id: "session-1".to_owned(),
            cwd: root.to_string_lossy().into_owned(),
            active_task_id: Some("task-1".to_owned()),
            updated_at: 13.5,
        })
        .expect("save context");

    assert_eq!(
        store.load_task("task-1").expect("load task").unwrap().state,
        "working"
    );
    assert_eq!(
        store
            .load_context("ctx-1")
            .expect("load context")
            .unwrap()
            .session_id,
        "session-1"
    );
}

#[test]
fn persistence_rejects_path_traversal_ids() {
    let root = temp_dir("path-traversal");
    let store = A2APersistenceStore::new(&root);

    let error = store
        .save_task(A2ATaskSnapshot {
            task_id: "../escape".to_owned(),
            context_id: "ctx-1".to_owned(),
            state: "working".to_owned(),
            output_text: Vec::new(),
            status_message: String::new(),
            updated_at: 1.0,
        })
        .expect_err("path traversal task id");

    assert_eq!(error, TaskStoreError::InvalidA2AId);
    assert!(!root.join("escape.json").exists());
}

#[test]
fn restore_marks_interrupted_runtime_states_and_persists_result() {
    for state in ["submitted", "working", "auth-required"] {
        let root = temp_dir(state);
        let store = A2APersistenceStore::new(&root);
        store
            .save_task(A2ATaskSnapshot {
                task_id: "task-1".to_owned(),
                context_id: "ctx-1".to_owned(),
                state: state.to_owned(),
                output_text: vec!["partial".to_owned()],
                status_message: String::new(),
                updated_at: 1.0,
            })
            .expect("save task");

        let restored = store
            .restore_task("task-1")
            .expect("restore task")
            .expect("snapshot");

        assert_eq!(restored.state, "interrupted");
        assert_eq!(restored.output_text, vec!["partial"]);
        assert!(restored.status_message.contains("cannot be revived"));
        assert_eq!(
            store.load_task("task-1").expect("load task").unwrap().state,
            "interrupted"
        );
    }
}

#[test]
fn restore_leaves_input_required_tasks_unchanged() {
    let root = temp_dir("input-required");
    let store = A2APersistenceStore::new(&root);
    store
        .save_task(A2ATaskSnapshot {
            task_id: "task-1".to_owned(),
            context_id: "ctx-1".to_owned(),
            state: "input-required".to_owned(),
            output_text: Vec::new(),
            status_message: "waiting".to_owned(),
            updated_at: 1.0,
        })
        .expect("save task");

    let restored = store
        .restore_task("task-1")
        .expect("restore task")
        .expect("snapshot");

    assert_eq!(restored.state, "input-required");
    assert_eq!(restored.status_message, "waiting");
}

#[test]
fn corrupt_task_files_are_skipped_when_listing() {
    let root = temp_dir("corrupt-task");
    let tasks = root.join("tasks");
    fs::create_dir_all(&tasks).expect("tasks dir");
    fs::write(tasks.join("bad.json"), "{broken").expect("write corrupt json");

    let store = A2APersistenceStore::new(&root);

    assert_eq!(store.list_tasks().expect("list tasks"), Vec::new());
}

#[test]
fn persistence_round_trips_route_snapshots_and_skips_invalid_routes() {
    let root = temp_dir("routes");
    let store = A2APersistenceStore::new(&root);

    store
        .save_routes(vec![A2ARouteSnapshot {
            name: "template".to_owned(),
            url: "http://template".to_owned(),
            skills: vec!["iac_generation".to_owned()],
            tags: vec!["ros".to_owned()],
        }])
        .expect("save routes");
    let text = fs::read_to_string(root.join("routes.json")).expect("routes json");
    assert_eq!(
        text,
        r#"{"routes": [{"name": "template", "skills": ["iac_generation"], "tags": ["ros"], "url": "http://template"}]}"#
    );

    assert_eq!(
        store.load_routes().expect("load routes"),
        vec![A2ARouteSnapshot {
            name: "template".to_owned(),
            url: "http://template".to_owned(),
            skills: vec!["iac_generation".to_owned()],
            tags: vec!["ros".to_owned()],
        }]
    );

    fs::write(
        root.join("routes.json"),
        r#"{"routes":[{"name":"ok","url":"http://ok","skills":["iac",3],"tags":["ros",false]},{"name":5,"url":"bad"}]}"#,
    )
    .expect("write mixed routes");

    assert_eq!(
        store.load_routes().expect("load routes"),
        vec![A2ARouteSnapshot {
            name: "ok".to_owned(),
            url: "http://ok".to_owned(),
            skills: vec!["iac".to_owned()],
            tags: vec!["ros".to_owned()],
        }]
    );
}

fn temp_dir(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "iac-code-a2a-persistence-{name}-{}-{nonce}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&root);
    root
}
