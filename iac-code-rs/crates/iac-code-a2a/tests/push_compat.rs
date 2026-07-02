use std::sync::atomic::{AtomicUsize, Ordering};

use iac_code_a2a::metrics::A2AMetrics;
use iac_code_a2a::push::{
    validate_push_callback_url, A2APushAuthentication, A2APushConfig, A2APushConfigStore,
    A2APushSender, InvalidPushNotificationConfigError, TaskPushNotificationConfig,
};
use iac_code_a2a::push_queue::LocalFileA2APushQueue;
use iac_code_protocol::json;

#[test]
fn push_config_accepts_http_and_https_public_callback_urls() {
    assert_eq!(
        validate_push_callback_url("https://callback.example/a2a"),
        Ok("https://callback.example/a2a".to_owned())
    );
    assert_eq!(
        validate_push_callback_url("http://callback.example:8080/a2a"),
        Ok("http://callback.example:8080/a2a".to_owned())
    );

    let config = A2APushConfig::new("task-1", "https://callback.example/a2a").expect("config");
    assert_eq!(config.task_id, "task-1");
    assert_eq!(config.callback_url, "https://callback.example/a2a");
}

#[test]
fn push_config_rejects_non_http_or_missing_host_urls() {
    for url in [
        "file:///tmp/callback",
        "mailto:test@example.com",
        "https:///missing-host",
        "https://callback.example:notaport/a2a",
        "https://callback.example:99999/a2a",
    ] {
        assert_eq!(
            validate_push_callback_url(url),
            Err(InvalidPushNotificationConfigError)
        );
    }
}

#[test]
fn push_config_rejects_localhost_names() {
    for url in [
        "http://localhost/a2a",
        "https://LOCALHOST/a2a",
        "https://api.localhost/a2a",
    ] {
        assert_eq!(
            validate_push_callback_url(url),
            Err(InvalidPushNotificationConfigError)
        );
    }
}

#[test]
fn push_config_rejects_private_or_local_ip_literals() {
    for url in [
        "http://127.0.0.1/a2a",
        "http://10.0.0.1/a2a",
        "http://172.16.0.1/a2a",
        "http://192.168.1.1/a2a",
        "http://169.254.1.1/a2a",
        "http://224.0.0.1/a2a",
        "http://0.0.0.0/a2a",
        "http://[::1]/a2a",
        "http://[fc00::1]/a2a",
    ] {
        assert_eq!(
            validate_push_callback_url(url),
            Err(InvalidPushNotificationConfigError),
            "{url}"
        );
    }
}

#[test]
fn push_sender_enqueues_standard_stream_response_without_persisting_auth_headers_like_python() {
    let root = temp_root("sender");
    let mut store = A2APushConfigStore::new(&root);
    let mut queue = LocalFileA2APushQueue::new(root.join("push_queue"));
    let mut config = TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a");
    config.token = "token-1".to_owned();
    config.authentication = Some(A2APushAuthentication::new("bearer", "secret"));
    store.set_info("", "task-1", config).expect("set config");
    let metrics = CountingMetrics::default();
    let payload = json::object([(
        "statusUpdate",
        json::object([
            ("taskId", json::string("task-1")),
            ("contextId", json::string("ctx-1")),
        ]),
    )]);

    {
        let mut sender = A2APushSender::new(&store, &mut queue).with_metrics(&metrics);
        assert_eq!(
            sender
                .send_notification("task-1", payload.clone())
                .expect("send notification"),
            1
        );
    }

    assert_eq!(metrics.enqueued(), 1);
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");
    assert_eq!(claimed.task_id, "task-1");
    assert_eq!(claimed.config_id, "cfg-1");
    assert_eq!(claimed.url, "https://callback.example/a2a");
    assert_eq!(claimed.payload, payload);
    assert!(claimed.headers.is_empty());
    assert!(!claimed.job_id.is_empty());
    assert_eq!(
        store
            .resolve_headers_for_dispatch("task-1", "cfg-1")
            .expect("headers"),
        std::collections::BTreeMap::from([
            ("X-A2A-Notification-Token".to_owned(), "token-1".to_owned()),
            ("Authorization".to_owned(), "Bearer secret".to_owned()),
        ])
    );
}

#[derive(Default)]
struct CountingMetrics {
    enqueued: AtomicUsize,
}

impl CountingMetrics {
    fn enqueued(&self) -> usize {
        self.enqueued.load(Ordering::SeqCst)
    }
}

impl A2AMetrics for CountingMetrics {
    fn record_push_enqueued(&self) {
        self.enqueued.fetch_add(1, Ordering::SeqCst);
    }
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root =
        std::env::temp_dir().join(format!("iac-code-a2a-push-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    root
}
