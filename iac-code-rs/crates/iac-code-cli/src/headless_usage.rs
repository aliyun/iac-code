use iac_code_config::paths::ConfigPaths;
use iac_code_core::SessionUsageStore;
use iac_code_exec::HeadlessRunResult;
use iac_code_protocol::StreamEvent;

pub(super) fn persist_headless_usage(
    paths: &ConfigPaths,
    cwd: &str,
    session_id: &str,
    result: &HeadlessRunResult,
    provider_key: &str,
    model: &str,
) {
    let usage_store = SessionUsageStore::new(paths.subdirs().projects);
    for event in &result.events {
        let StreamEvent::MessageEnd(message_end) = event else {
            continue;
        };
        let _ = usage_store.append(
            cwd,
            session_id,
            &message_end.usage,
            Some(provider_key),
            Some(model),
        );
    }
}
