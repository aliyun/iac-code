use crate::{desktop_text, is_bundled_origin, sidecar, AppState};
use serde::Serialize;
use std::path::PathBuf;
use tauri::{AppHandle, State, WebviewWindow};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProjectSelection {
    path: String,
}

fn caller_url(window: &WebviewWindow) -> Result<url::Url, String> {
    window.url().map_err(|error| error.to_string())
}

fn require_local(window: &WebviewWindow) -> Result<(), String> {
    let url = caller_url(window)?;
    is_bundled_origin(&url)
        .then_some(())
        .ok_or_else(|| "command is only available to the bundled bootstrap".to_string())
}

fn require_remote(window: &WebviewWindow, state: &AppState) -> Result<(), String> {
    let url = caller_url(window)?;
    let lifecycle = state.lifecycle.lock();
    let expected = lifecycle
        .healthy_origin
        .as_ref()
        .ok_or_else(|| "the Desktop sidecar is not healthy".to_string())?;
    (url.scheme() == expected.scheme()
        && url.host_str() == expected.host_str()
        && url.port_or_known_default() == expected.port_or_known_default())
    .then_some(())
    .ok_or_else(|| "command caller does not match the healthy Desktop origin".to_string())
}

#[tauri::command]
pub async fn complete_bootstrap_check(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    bootstrap_operation_id: Uuid,
    compatible: bool,
) -> Result<bool, String> {
    let page_mode = caller_url(&window)?
        .query_pairs()
        .find_map(|(key, value)| (key == "mode").then(|| value.into_owned()));
    require_local(&window)?;
    state
        .lifecycle
        .lock()
        .complete_bootstrap_check(bootstrap_operation_id, compatible)
        .map_err(str::to_string)?;
    if !compatible || matches!(page_mode.as_deref(), Some("recovery" | "stopping")) {
        return Ok(false);
    }
    if state.startup_recovery_error.is_some() {
        return Ok(false);
    }
    let recent_project = state.host_state.lock().state().recent_project.clone();
    let Some(project) = recent_project.filter(|path| path.is_dir()) else {
        return Ok(false);
    };
    let start_app = app.clone();
    let port = tauri::async_runtime::spawn_blocking(move || sidecar::start(&start_app, &project))
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())?;
    window
        .navigate(
            format!("http://127.0.0.1:{port}/")
                .parse()
                .map_err(|error: url::ParseError| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
    Ok(true)
}

#[tauri::command]
pub async fn select_project_directory(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    bootstrap_operation_id: Option<Uuid>,
) -> Result<Option<ProjectSelection>, String> {
    let local = is_bundled_origin(&caller_url(&window)?);
    let mut local_operation = None;
    let mut remote_operation = None;
    if local {
        let operation_id =
            bootstrap_operation_id.ok_or_else(|| "bootstrap operation is required".to_string())?;
        state
            .lifecycle
            .lock()
            .begin_local_picker(operation_id)
            .map_err(str::to_string)?;
        local_operation = Some(operation_id);
    } else {
        require_remote(&window, &state)?;
        let source_generation =
            sidecar::current_generation(&app).map_err(|error| error.to_string())?;
        let picker_operation_id = state
            .lifecycle
            .lock()
            .begin_remote_picker(source_generation)
            .map_err(str::to_string)?;
        remote_operation = Some((picker_operation_id, source_generation));
    }
    let dialog_app = app.clone();
    let picker_title = desktop_text(&state.language, "select_project").to_string();
    let selected_result = tauri::async_runtime::spawn_blocking(move || {
        dialog_app
            .dialog()
            .file()
            .set_title(picker_title)
            .blocking_pick_folder()
    })
    .await;
    let selected = match selected_result {
        Ok(selected) => selected,
        Err(error) => {
            if let Some(operation_id) = local_operation {
                state.lifecycle.lock().cancel_local_picker(operation_id);
            }
            if let Some((picker_operation_id, source_generation)) = remote_operation {
                state
                    .lifecycle
                    .lock()
                    .cancel_remote_picker(picker_operation_id, source_generation);
            }
            return Err(error.to_string());
        }
    };
    let Some(selected) = selected else {
        if let Some(operation_id) = local_operation {
            state.lifecycle.lock().cancel_local_picker(operation_id);
        }
        if let Some((picker_operation_id, source_generation)) = remote_operation {
            state
                .lifecycle
                .lock()
                .cancel_remote_picker(picker_operation_id, source_generation);
        }
        return Ok(None);
    };
    let project_result: Result<PathBuf, String> = selected
        .into_path()
        .map_err(|error| error.to_string())
        .and_then(|project| project.canonicalize().map_err(|error| error.to_string()));
    let project = match project_result {
        Ok(project) => project,
        Err(error) => {
            if let Some(operation_id) = local_operation {
                state.lifecycle.lock().cancel_local_picker(operation_id);
            }
            if let Some((picker_operation_id, source_generation)) = remote_operation {
                state
                    .lifecycle
                    .lock()
                    .cancel_remote_picker(picker_operation_id, source_generation);
            }
            return Err(error);
        }
    };
    if !project.is_dir() {
        if let Some(operation_id) = local_operation {
            state.lifecycle.lock().cancel_local_picker(operation_id);
        }
        if let Some((picker_operation_id, source_generation)) = remote_operation {
            state
                .lifecycle
                .lock()
                .cancel_remote_picker(picker_operation_id, source_generation);
        }
        return Err("selected project is not a directory".to_string());
    }
    if let Some((picker_operation_id, source_generation)) = remote_operation {
        if let Err(error) = state
            .lifecycle
            .lock()
            .commit_remote_picker(picker_operation_id, source_generation)
        {
            state
                .lifecycle
                .lock()
                .cancel_remote_picker(picker_operation_id, source_generation);
            return Err(error.to_string());
        }
        let commit_app = app.clone();
        let commit_project = project.clone();
        let commit_result = match tauri::async_runtime::spawn_blocking(move || {
            sidecar::set_default_project(
                &commit_app,
                &commit_project,
                picker_operation_id,
                source_generation,
            )
        })
        .await
        {
            Ok(result) => result.map_err(|error| error.to_string()),
            Err(error) => Err(error.to_string()),
        };
        if let Err(error) = commit_result {
            state
                .lifecycle
                .lock()
                .cancel_remote_picker(picker_operation_id, source_generation);
            return Err(error);
        }
        let persist_result = {
            let mut host_state = state.host_state.lock();
            host_state.save_project(project.clone())
        };
        if let Err(_error) = persist_result {
            state
                .lifecycle
                .lock()
                .cancel_remote_picker(picker_operation_id, source_generation);
            let stop_app = app.clone();
            let _ = tauri::async_runtime::spawn_blocking(move || {
                sidecar::force_stop_container(&stop_app)
            })
            .await;
            let message = desktop_text(&state.language, "project_persist_failed").to_string();
            sidecar::show_recovery_page(&app, &message);
            return Err(message);
        }
        state
            .lifecycle
            .lock()
            .finish_remote_picker(picker_operation_id, source_generation)
            .map_err(str::to_string)?;
    } else {
        let operation_id =
            bootstrap_operation_id.ok_or_else(|| "bootstrap operation is required".to_string())?;
        let start_app = app.clone();
        let start_project = project.clone();
        let port = tauri::async_runtime::spawn_blocking(move || {
            sidecar::start_from_bootstrap(&start_app, &start_project, operation_id)
        })
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())?;
        let persist_result = {
            let mut host_state = state.host_state.lock();
            host_state.save_project(project.clone())
        };
        if let Err(_error) = persist_result {
            let stop_app = app.clone();
            let _ = tauri::async_runtime::spawn_blocking(move || {
                sidecar::force_stop_container(&stop_app)
            })
            .await;
            let message = desktop_text(&state.language, "project_persist_failed").to_string();
            sidecar::show_recovery_page(&app, &message);
            return Err(message);
        }
        window
            .navigate(
                format!("http://127.0.0.1:{port}/")
                    .parse()
                    .map_err(|error: url::ParseError| error.to_string())?,
            )
            .map_err(|error| error.to_string())?;
    }
    Ok(Some(ProjectSelection {
        path: project.to_string_lossy().into_owned(),
    }))
}

#[tauri::command]
pub async fn retry_start_sidecar(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    replace_occupied_port: Option<bool>,
) -> Result<(), String> {
    require_local(&window)?;
    let project = state
        .host_state
        .lock()
        .state()
        .recent_project
        .clone()
        .filter(|path| path.is_dir())
        .ok_or_else(|| "no Desktop project is selected".to_string())?;
    let start_app = app.clone();
    let replace_occupied_port = replace_occupied_port.unwrap_or(false);
    let port = tauri::async_runtime::spawn_blocking(move || {
        sidecar::start_with_options(&start_app, &project, replace_occupied_port)
    })
    .await
    .map_err(|error| error.to_string())?
    .map_err(|error| error.to_string())?;
    window
        .navigate(
            format!("http://127.0.0.1:{port}/")
                .parse()
                .map_err(|error: url::ParseError| error.to_string())?,
        )
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn quit_app(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    require_local(&window)?;
    crate::request_close(&app);
    Ok(())
}

#[tauri::command]
pub async fn confirm_secret_reveal(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    kind: String,
    id: String,
    field_label: String,
) -> Result<bool, String> {
    require_remote(&window, &state)?;
    if kind.len() > 64 || id.len() > 128 || field_label.is_empty() || field_label.len() > 128 {
        return Err("secret field description is invalid".to_string());
    }
    let language = state.language.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let message = match language.as_str() {
            "zh" => format!("显示 {} 的值？", field_label),
            "es" => format!("¿Mostrar el valor de {}?", field_label),
            "fr" => format!("Afficher la valeur de {} ?", field_label),
            "de" => format!("Den Wert von {} anzeigen?", field_label),
            "ja" => format!("{} の値を表示しますか？", field_label),
            "pt" => format!("Mostrar o valor de {}?", field_label),
            _ => format!("Show the value of {field_label}?"),
        };
        app.dialog()
            .message(message)
            .title(desktop_text(&language, "reveal_secret"))
            .buttons(MessageDialogButtons::OkCancel)
            .blocking_show()
    })
    .await
    .map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn restart_sidecar(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    require_remote(&window, &state)?;
    let localized_error = desktop_text(&state.language, "runtime_restart_failed").to_string();
    let restart_app = app.clone();
    let port = tauri::async_runtime::spawn_blocking(move || sidecar::restart(&restart_app))
        .await
        .map_err(|_error| localized_error.clone())?
        .map_err(|_error| localized_error.clone())?;
    window
        .navigate(
            format!("http://127.0.0.1:{port}/")
                .parse()
                .map_err(|_error: url::ParseError| localized_error.clone())?,
        )
        .map_err(|_error| localized_error)
}

#[tauri::command]
pub fn open_diagnostics_directory(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let url = caller_url(&window)?;
    if !is_bundled_origin(&url) {
        require_remote(&window, &state)?;
    }
    app.opener()
        .open_path(state.paths.log_dir.to_string_lossy(), None::<&str>)
        .map_err(|error| error.to_string())
}

#[tauri::command]
pub fn open_external_url(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    url: String,
) -> Result<(), String> {
    require_remote(&window, &state)?;
    let parsed = url::Url::parse(&url).map_err(|_| "external URL is invalid".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err("external URL must use HTTP or HTTPS".to_string());
    }
    app.opener()
        .open_url(parsed.as_str(), None::<&str>)
        .map_err(|error| error.to_string())
}

#[cfg(feature = "updater")]
#[tauri::command]
pub async fn check_update(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<crate::updater::UpdateStatus, String> {
    require_remote(&window, &state)?;
    crate::updater::check(&app)
        .await
        .map_err(|_error| desktop_text(&state.language, "update_recovery_failed").to_string())
}

#[cfg(feature = "updater")]
#[tauri::command]
pub fn dismiss_update(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
    version: String,
) -> Result<(), String> {
    require_remote(&window, &state)?;
    let version = version.trim();
    if version.is_empty() || version.len() > 128 {
        return Err("Desktop update version is invalid".to_string());
    }
    state
        .host_state
        .lock()
        .save_dismissed_update_version(version.to_string())
        .map_err(|error| error.to_string())?;
    crate::updater::invalidate_all(&app);
    Ok(())
}

#[cfg(feature = "updater")]
#[tauri::command]
pub async fn download_update(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<crate::updater::UpdateStatus, String> {
    require_remote(&window, &state)?;
    crate::updater::download(&app)
        .await
        .map_err(|_error| desktop_text(&state.language, "update_recovery_failed").to_string())
}

#[cfg(feature = "updater")]
#[tauri::command]
pub async fn install_update(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    require_remote(&window, &state)?;
    crate::updater::install(&app)
        .await
        .map_err(|_error| desktop_text(&state.language, "update_recovery_failed").to_string())
}
