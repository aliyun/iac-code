mod commands;
mod control;
mod host_state;
mod lifecycle;
mod sidecar;
#[cfg(feature = "updater")]
mod updater;
#[cfg(windows)]
use iac_code_desktop_helpers::windows_update;

use anyhow::{Context, Result};
use host_state::{
    restored_window_placement, HostStateStore, LogicalWindowSize, MonitorWorkArea,
    PhysicalWindowPosition, PhysicalWindowSize, WindowState, MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH,
};
use lifecycle::{LifecycleCoordinator, LifecycleState};
use parking_lot::Mutex;
use serde::Deserialize;
use serde_json::json;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::menu::{
    AboutMetadata, Menu, PredefinedMenuItem, Submenu, HELP_SUBMENU_ID, WINDOW_SUBMENU_ID,
};
use tauri::webview::{NewWindowResponse, WebviewWindowBuilder};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WindowEvent};
use tauri_plugin_opener::OpenerExt;

pub struct HostPaths {
    pub family_root: PathBuf,
    pub host_state_dir: PathBuf,
    pub runtime_dir: PathBuf,
    pub log_dir: PathBuf,
    pub install_lock_dir: PathBuf,
}

impl HostPaths {
    fn create(app: &tauri::App) -> Result<Self> {
        let family_root = app
            .path()
            .app_local_data_dir()
            .context("resolve Desktop product-family directory")?;
        let paths = Self::from_family_root(family_root, sidecar::distribution_channel());
        paths.create_directories()?;
        Ok(paths)
    }

    fn from_family_root(family_root: PathBuf, distribution_channel: &str) -> Self {
        let host_state_dir = family_root.join(distribution_channel);
        let runtime_dir = host_state_dir.join("runtime");
        let log_dir = host_state_dir.join("logs");
        let install_lock_dir = family_root.join("iac-code-desktop-install-locks");
        Self {
            family_root,
            host_state_dir,
            runtime_dir,
            log_dir,
            install_lock_dir,
        }
    }

    fn create_directories(&self) -> Result<()> {
        for path in [
            &self.family_root,
            &self.host_state_dir,
            &self.runtime_dir,
            &self.log_dir,
            &self.install_lock_dir,
        ] {
            fs::create_dir_all(path)
                .with_context(|| format!("create Desktop directory {}", path.display()))?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
            }
        }
        Ok(())
    }
}

pub struct AppState {
    pub paths: HostPaths,
    pub host_state: Mutex<HostStateStore>,
    pub window_state_cache: Mutex<Option<WindowState>>,
    pub lifecycle: Mutex<LifecycleCoordinator>,
    pub sidecar: Mutex<Option<sidecar::SidecarHandle>>,
    pub close_in_progress: AtomicBool,
    pub language: String,
    pub startup_recovery_error: Option<String>,
    #[cfg(feature = "updater")]
    pub updater: updater::UpdaterCoordinator,
}

#[derive(Default, Deserialize)]
struct DesktopSettings {
    ui: Option<DesktopUiSettings>,
    appearance: Option<DesktopAppearanceSettings>,
}

#[derive(Deserialize)]
struct DesktopUiSettings {
    language: Option<String>,
}

#[derive(Deserialize)]
struct DesktopAppearanceSettings {
    theme: Option<String>,
}

fn supported_language(value: &str) -> Option<&'static str> {
    let primary = value
        .trim()
        .split(':')
        .next()?
        .split(['_', '-', '.', '@'])
        .next()?
        .to_ascii_lowercase();
    match primary.as_str() {
        "en" => Some("en"),
        "zh" => Some("zh"),
        "es" => Some("es"),
        "fr" => Some("fr"),
        "de" => Some("de"),
        "ja" => Some("ja"),
        "pt" => Some("pt"),
        _ => None,
    }
}

fn language_from_values<'a>(values: impl IntoIterator<Item = &'a str>) -> Option<String> {
    values
        .into_iter()
        .find_map(supported_language)
        .map(str::to_string)
}

#[cfg(windows)]
fn windows_user_language() -> Option<String> {
    use windows::Win32::Globalization::GetUserDefaultLocaleName;

    let mut locale = [0_u16; 85];
    let length = unsafe { GetUserDefaultLocaleName(&mut locale) };
    if length <= 1 {
        return None;
    }
    let value = String::from_utf16_lossy(&locale[..(length as usize - 1)]);
    supported_language(&value).map(str::to_string)
}

#[cfg(target_os = "macos")]
fn macos_user_language() -> Option<String> {
    use objc2_foundation::NSLocale;

    NSLocale::preferredLanguages()
        .iter()
        .find_map(|language| supported_language(&language.to_string()).map(str::to_string))
}

fn system_language() -> String {
    let values = ["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"]
        .into_iter()
        .filter_map(|name| std::env::var(name).ok())
        .collect::<Vec<_>>();
    if let Some(language) = language_from_values(values.iter().map(String::as_str)) {
        return language;
    }
    #[cfg(windows)]
    if let Some(language) = windows_user_language() {
        return language;
    }
    #[cfg(target_os = "macos")]
    if let Some(language) = macos_user_language() {
        return language;
    }
    "en".to_string()
}

fn configured_language(app: &AppHandle) -> String {
    let config_dir = sidecar::config_dir(app).ok();
    let language = config_dir
        .and_then(|directory| fs::read(directory.join("settings.yml")).ok())
        .and_then(|raw| serde_yaml::from_slice::<DesktopSettings>(&raw).ok())
        .and_then(|settings| settings.ui.and_then(|ui| ui.language));
    language
        .as_deref()
        .and_then(supported_language)
        .map(str::to_string)
        .unwrap_or_else(system_language)
}

fn supported_theme(value: &str) -> Option<&'static str> {
    match value.trim() {
        "graphite" => Some("graphite"),
        "midnight" => Some("midnight"),
        "evergreen" => Some("evergreen"),
        "sepia" => Some("sepia"),
        "ivory" => Some("ivory"),
        _ => None,
    }
}

pub(crate) fn configured_theme(app: &AppHandle) -> String {
    let config_dir = sidecar::config_dir(app).ok();
    config_dir
        .and_then(|directory| fs::read(directory.join("settings.yml")).ok())
        .and_then(|raw| serde_yaml::from_slice::<DesktopSettings>(&raw).ok())
        .and_then(|settings| settings.appearance.and_then(|appearance| appearance.theme))
        .as_deref()
        .and_then(supported_theme)
        .unwrap_or("graphite")
        .to_string()
}

pub fn desktop_text(language: &str, key: &str) -> &'static str {
    match (language, key) {
        ("zh", "quit_title") => "退出 iac-code",
        ("zh", "force_quit") => "强制退出",
        ("zh", "return_app") => "返回应用",
        ("zh", "wait") => "等待",
        ("zh", "active_work") => "iac-code 仍在完成任务。你可以等待、强制退出或返回应用。",
        ("zh", "close_failed") => "本地运行时无法正常关闭。是否强制退出？",
        ("zh", "select_project") => "选择 iac-code 项目",
        ("zh", "reveal_secret") => "查看密钥",
        ("zh", "project_persist_failed") => "无法保存所选项目。请打开诊断目录查看详情。",
        ("zh", "runtime_stopped") => "本地运行时意外停止。请重试或打开诊断目录查看详情。",
        ("zh", "runtime_restart_failed") => "本地运行时无法重新启动。请重试或打开诊断目录查看详情。",
        ("zh", "update_recovery_failed") => "桌面更新未能完成。请重试本地运行时或重新安装应用。",
        ("zh", "menu_file") => "文件",
        ("zh", "menu_edit") => "编辑",
        ("zh", "menu_view") => "视图",
        ("zh", "menu_window") => "窗口",
        ("zh", "menu_help") => "帮助",
        ("zh", "menu_about") => "关于 iac-code",
        ("zh", "menu_services") => "服务",
        ("zh", "menu_hide") => "隐藏 iac-code",
        ("zh", "menu_hide_others") => "隐藏其他应用",
        ("zh", "menu_quit") => "退出 iac-code",
        ("zh", "menu_close_window") => "关闭窗口",
        ("zh", "menu_undo") => "撤销",
        ("zh", "menu_redo") => "重做",
        ("zh", "menu_cut") => "剪切",
        ("zh", "menu_copy") => "复制",
        ("zh", "menu_paste") => "粘贴",
        ("zh", "menu_select_all") => "全选",
        ("zh", "menu_fullscreen") => "全屏",
        ("zh", "menu_minimize") => "最小化",
        ("zh", "menu_maximize") => "最大化",
        ("es", "quit_title") => "Salir de iac-code",
        ("es", "force_quit") => "Forzar salida",
        ("es", "return_app") => "Volver a la aplicación",
        ("es", "wait") => "Esperar",
        ("es", "active_work") => "iac-code aún está terminando tareas. Puedes esperar, forzar la salida o volver.",
        ("es", "close_failed") => "El entorno local no se pudo cerrar correctamente. ¿Forzar la salida?",
        ("es", "select_project") => "Seleccionar proyecto de iac-code",
        ("es", "reveal_secret") => "Mostrar secreto",
        ("es", "project_persist_failed") => "No se pudo guardar el proyecto seleccionado. Abre los diagnósticos para obtener más información.",
        ("es", "runtime_stopped") => "El entorno local se detuvo inesperadamente. Inténtalo de nuevo o abre los diagnósticos.",
        ("es", "runtime_restart_failed") => "No se pudo reiniciar el entorno local. Inténtalo de nuevo o abre los diagnósticos.",
        ("es", "update_recovery_failed") => "La actualización de escritorio no pudo completarse. Reintenta el entorno local o reinstala la aplicación.",
        ("es", "menu_file") => "Archivo",
        ("es", "menu_edit") => "Edición",
        ("es", "menu_view") => "Ver",
        ("es", "menu_window") => "Ventana",
        ("es", "menu_help") => "Ayuda",
        ("es", "menu_about") => "Acerca de iac-code",
        ("es", "menu_services") => "Servicios",
        ("es", "menu_hide") => "Ocultar iac-code",
        ("es", "menu_hide_others") => "Ocultar otros",
        ("es", "menu_quit") => "Salir de iac-code",
        ("es", "menu_close_window") => "Cerrar ventana",
        ("es", "menu_undo") => "Deshacer",
        ("es", "menu_redo") => "Rehacer",
        ("es", "menu_cut") => "Cortar",
        ("es", "menu_copy") => "Copiar",
        ("es", "menu_paste") => "Pegar",
        ("es", "menu_select_all") => "Seleccionar todo",
        ("es", "menu_fullscreen") => "Pantalla completa",
        ("es", "menu_minimize") => "Minimizar",
        ("es", "menu_maximize") => "Maximizar",
        ("fr", "quit_title") => "Quitter iac-code",
        ("fr", "force_quit") => "Forcer à quitter",
        ("fr", "return_app") => "Revenir à l’application",
        ("fr", "wait") => "Attendre",
        ("fr", "active_work") => "iac-code termine encore des tâches. Vous pouvez attendre, forcer l’arrêt ou revenir.",
        ("fr", "close_failed") => "Le service local n’a pas pu s’arrêter correctement. Forcer l’arrêt ?",
        ("fr", "select_project") => "Sélectionner un projet iac-code",
        ("fr", "reveal_secret") => "Afficher le secret",
        ("fr", "project_persist_failed") => "Impossible d’enregistrer le projet sélectionné. Ouvrez les diagnostics pour plus d’informations.",
        ("fr", "runtime_stopped") => "Le service local s’est arrêté de manière inattendue. Réessayez ou ouvrez les diagnostics.",
        ("fr", "runtime_restart_failed") => "Le service local n’a pas pu redémarrer. Réessayez ou ouvrez les diagnostics.",
        ("fr", "update_recovery_failed") => "La mise à jour de l’application n’a pas pu aboutir. Réessayez le service local ou réinstallez l’application.",
        ("fr", "menu_file") => "Fichier",
        ("fr", "menu_edit") => "Édition",
        ("fr", "menu_view") => "Affichage",
        ("fr", "menu_window") => "Fenêtre",
        ("fr", "menu_help") => "Aide",
        ("fr", "menu_about") => "À propos de iac-code",
        ("fr", "menu_services") => "Services",
        ("fr", "menu_hide") => "Masquer iac-code",
        ("fr", "menu_hide_others") => "Masquer les autres",
        ("fr", "menu_quit") => "Quitter iac-code",
        ("fr", "menu_close_window") => "Fermer la fenêtre",
        ("fr", "menu_undo") => "Annuler",
        ("fr", "menu_redo") => "Rétablir",
        ("fr", "menu_cut") => "Couper",
        ("fr", "menu_copy") => "Copier",
        ("fr", "menu_paste") => "Coller",
        ("fr", "menu_select_all") => "Tout sélectionner",
        ("fr", "menu_fullscreen") => "Plein écran",
        ("fr", "menu_minimize") => "Réduire",
        ("fr", "menu_maximize") => "Agrandir",
        ("de", "quit_title") => "iac-code beenden",
        ("de", "force_quit") => "Beenden erzwingen",
        ("de", "return_app") => "Zurück zur App",
        ("de", "wait") => "Warten",
        ("de", "active_work") => "iac-code schließt noch Aufgaben ab. Sie können warten, das Beenden erzwingen oder zurückkehren.",
        ("de", "close_failed") => "Die lokale Laufzeit konnte nicht sauber beendet werden. Beenden erzwingen?",
        ("de", "select_project") => "iac-code-Projekt auswählen",
        ("de", "reveal_secret") => "Geheimnis anzeigen",
        ("de", "project_persist_failed") => "Das ausgewählte Projekt konnte nicht gespeichert werden. Öffnen Sie die Diagnose, um Details anzuzeigen.",
        ("de", "runtime_stopped") => "Die lokale Laufzeit wurde unerwartet beendet. Versuchen Sie es erneut oder öffnen Sie die Diagnose.",
        ("de", "runtime_restart_failed") => "Die lokale Laufzeit konnte nicht neu gestartet werden. Versuchen Sie es erneut oder öffnen Sie die Diagnose.",
        ("de", "update_recovery_failed") => "Das Desktop-Update konnte nicht abgeschlossen werden. Starten Sie die lokale Laufzeit erneut oder installieren Sie die App neu.",
        ("de", "menu_file") => "Datei",
        ("de", "menu_edit") => "Bearbeiten",
        ("de", "menu_view") => "Ansicht",
        ("de", "menu_window") => "Fenster",
        ("de", "menu_help") => "Hilfe",
        ("de", "menu_about") => "Über iac-code",
        ("de", "menu_services") => "Dienste",
        ("de", "menu_hide") => "iac-code ausblenden",
        ("de", "menu_hide_others") => "Andere ausblenden",
        ("de", "menu_quit") => "iac-code beenden",
        ("de", "menu_close_window") => "Fenster schließen",
        ("de", "menu_undo") => "Rückgängig",
        ("de", "menu_redo") => "Wiederholen",
        ("de", "menu_cut") => "Ausschneiden",
        ("de", "menu_copy") => "Kopieren",
        ("de", "menu_paste") => "Einfügen",
        ("de", "menu_select_all") => "Alles auswählen",
        ("de", "menu_fullscreen") => "Vollbild",
        ("de", "menu_minimize") => "Minimieren",
        ("de", "menu_maximize") => "Maximieren",
        ("ja", "quit_title") => "iac-code を終了",
        ("ja", "force_quit") => "強制終了",
        ("ja", "return_app") => "アプリに戻る",
        ("ja", "wait") => "待機",
        ("ja", "active_work") => "iac-code はタスクを完了中です。待機、強制終了、またはアプリに戻ることができます。",
        ("ja", "close_failed") => "ローカルランタイムを正常に終了できませんでした。強制終了しますか？",
        ("ja", "select_project") => "iac-code プロジェクトを選択",
        ("ja", "reveal_secret") => "シークレットを表示",
        ("ja", "project_persist_failed") => "選択したプロジェクトを保存できませんでした。詳細は診断を開いて確認してください。",
        ("ja", "runtime_stopped") => "ローカルランタイムが予期せず停止しました。再試行するか、診断を開いてください。",
        ("ja", "runtime_restart_failed") => "ローカルランタイムを再起動できませんでした。再試行するか、診断を開いてください。",
        ("ja", "update_recovery_failed") => "デスクトップの更新を完了できませんでした。ローカルランタイムを再試行するか、アプリを再インストールしてください。",
        ("ja", "menu_file") => "ファイル",
        ("ja", "menu_edit") => "編集",
        ("ja", "menu_view") => "表示",
        ("ja", "menu_window") => "ウインドウ",
        ("ja", "menu_help") => "ヘルプ",
        ("ja", "menu_about") => "iac-code について",
        ("ja", "menu_services") => "サービス",
        ("ja", "menu_hide") => "iac-code を隠す",
        ("ja", "menu_hide_others") => "ほかを隠す",
        ("ja", "menu_quit") => "iac-code を終了",
        ("ja", "menu_close_window") => "ウインドウを閉じる",
        ("ja", "menu_undo") => "取り消す",
        ("ja", "menu_redo") => "やり直す",
        ("ja", "menu_cut") => "切り取り",
        ("ja", "menu_copy") => "コピー",
        ("ja", "menu_paste") => "ペースト",
        ("ja", "menu_select_all") => "すべてを選択",
        ("ja", "menu_fullscreen") => "フルスクリーン",
        ("ja", "menu_minimize") => "最小化",
        ("ja", "menu_maximize") => "最大化",
        ("pt", "quit_title") => "Sair do iac-code",
        ("pt", "force_quit") => "Forçar saída",
        ("pt", "return_app") => "Voltar ao aplicativo",
        ("pt", "wait") => "Aguardar",
        ("pt", "active_work") => "O iac-code ainda está concluindo tarefas. Você pode aguardar, forçar a saída ou voltar.",
        ("pt", "close_failed") => "O ambiente local não pôde ser encerrado corretamente. Forçar saída?",
        ("pt", "select_project") => "Selecionar projeto do iac-code",
        ("pt", "reveal_secret") => "Mostrar segredo",
        ("pt", "project_persist_failed") => "Não foi possível salvar o projeto selecionado. Abra os diagnósticos para ver os detalhes.",
        ("pt", "runtime_stopped") => "O ambiente local parou inesperadamente. Tente novamente ou abra os diagnósticos.",
        ("pt", "runtime_restart_failed") => "Não foi possível reiniciar o ambiente local. Tente novamente ou abra os diagnósticos.",
        ("pt", "update_recovery_failed") => "Não foi possível concluir a atualização do aplicativo. Tente novamente o ambiente local ou reinstale o aplicativo.",
        ("pt", "menu_file") => "Arquivo",
        ("pt", "menu_edit") => "Editar",
        ("pt", "menu_view") => "Visualizar",
        ("pt", "menu_window") => "Janela",
        ("pt", "menu_help") => "Ajuda",
        ("pt", "menu_about") => "Sobre o iac-code",
        ("pt", "menu_services") => "Serviços",
        ("pt", "menu_hide") => "Ocultar iac-code",
        ("pt", "menu_hide_others") => "Ocultar outros",
        ("pt", "menu_quit") => "Sair do iac-code",
        ("pt", "menu_close_window") => "Fechar janela",
        ("pt", "menu_undo") => "Desfazer",
        ("pt", "menu_redo") => "Refazer",
        ("pt", "menu_cut") => "Recortar",
        ("pt", "menu_copy") => "Copiar",
        ("pt", "menu_paste") => "Colar",
        ("pt", "menu_select_all") => "Selecionar tudo",
        ("pt", "menu_fullscreen") => "Tela cheia",
        ("pt", "menu_minimize") => "Minimizar",
        ("pt", "menu_maximize") => "Maximizar",
        (_, "quit_title") => "Quit iac-code",
        (_, "force_quit") => "Force quit",
        (_, "return_app") => "Return to app",
        (_, "wait") => "Wait",
        (_, "active_work") => "iac-code is still finishing work. You can wait, force quit, or return to the app.",
        (_, "close_failed") => "The local runtime could not close cleanly. Force quit?",
        (_, "select_project") => "Select an iac-code project",
        (_, "reveal_secret") => "Reveal secret",
        (_, "project_persist_failed") => "The selected project could not be saved. Open diagnostics for details.",
        (_, "runtime_stopped") => "The local runtime stopped unexpectedly. Retry or open diagnostics for details.",
        (_, "runtime_restart_failed") => "The local runtime could not restart. Retry or open diagnostics for details.",
        (_, "update_recovery_failed") => "The Desktop update could not be completed. Retry the local runtime or reinstall the application.",
        (_, "menu_file") => "File",
        (_, "menu_edit") => "Edit",
        (_, "menu_view") => "View",
        (_, "menu_window") => "Window",
        (_, "menu_help") => "Help",
        (_, "menu_about") => "About iac-code",
        (_, "menu_services") => "Services",
        (_, "menu_hide") => "Hide iac-code",
        (_, "menu_hide_others") => "Hide Others",
        (_, "menu_quit") => "Quit iac-code",
        (_, "menu_close_window") => "Close Window",
        (_, "menu_undo") => "Undo",
        (_, "menu_redo") => "Redo",
        (_, "menu_cut") => "Cut",
        (_, "menu_copy") => "Copy",
        (_, "menu_paste") => "Paste",
        (_, "menu_select_all") => "Select All",
        (_, "menu_fullscreen") => "Enter Full Screen",
        (_, "menu_minimize") => "Minimize",
        (_, "menu_maximize") => "Maximize",
        _ => "iac-code",
    }
}

fn localized_menu(app: &AppHandle, language: &str) -> tauri::Result<Menu<tauri::Wry>> {
    let text = |key| desktop_text(language, key);
    let package = app.package_info();
    let config = app.config();
    let about_metadata = AboutMetadata {
        name: Some(package.name.clone()),
        version: Some(package.version.to_string()),
        copyright: config.bundle.copyright.clone(),
        authors: config
            .bundle
            .publisher
            .clone()
            .map(|publisher| vec![publisher]),
        ..Default::default()
    };

    let window_menu = Submenu::with_id_and_items(
        app,
        WINDOW_SUBMENU_ID,
        text("menu_window"),
        true,
        &[
            &PredefinedMenuItem::minimize(app, Some(text("menu_minimize")))?,
            &PredefinedMenuItem::maximize(app, Some(text("menu_maximize")))?,
            #[cfg(target_os = "macos")]
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::close_window(app, Some(text("menu_close_window")))?,
        ],
    )?;
    let help_menu = Submenu::with_id_and_items(
        app,
        HELP_SUBMENU_ID,
        text("menu_help"),
        true,
        &[
            #[cfg(not(target_os = "macos"))]
            &PredefinedMenuItem::about(
                app,
                Some(text("menu_about")),
                Some(about_metadata.clone()),
            )?,
        ],
    )?;

    Menu::with_items(
        app,
        &[
            #[cfg(target_os = "macos")]
            &Submenu::with_items(
                app,
                package.name.clone(),
                true,
                &[
                    &PredefinedMenuItem::about(
                        app,
                        Some(text("menu_about")),
                        Some(about_metadata),
                    )?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::services(app, Some(text("menu_services")))?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::hide(app, Some(text("menu_hide")))?,
                    &PredefinedMenuItem::hide_others(app, Some(text("menu_hide_others")))?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::quit(app, Some(text("menu_quit")))?,
                ],
            )?,
            #[cfg(not(any(
                target_os = "linux",
                target_os = "dragonfly",
                target_os = "freebsd",
                target_os = "netbsd",
                target_os = "openbsd"
            )))]
            &Submenu::with_items(
                app,
                text("menu_file"),
                true,
                &[
                    &PredefinedMenuItem::close_window(app, Some(text("menu_close_window")))?,
                    #[cfg(not(target_os = "macos"))]
                    &PredefinedMenuItem::quit(app, Some(text("menu_quit")))?,
                ],
            )?,
            &Submenu::with_items(
                app,
                text("menu_edit"),
                true,
                &[
                    &PredefinedMenuItem::undo(app, Some(text("menu_undo")))?,
                    &PredefinedMenuItem::redo(app, Some(text("menu_redo")))?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, Some(text("menu_cut")))?,
                    &PredefinedMenuItem::copy(app, Some(text("menu_copy")))?,
                    &PredefinedMenuItem::paste(app, Some(text("menu_paste")))?,
                    &PredefinedMenuItem::select_all(app, Some(text("menu_select_all")))?,
                ],
            )?,
            #[cfg(target_os = "macos")]
            &Submenu::with_items(
                app,
                text("menu_view"),
                true,
                &[&PredefinedMenuItem::fullscreen(
                    app,
                    Some(text("menu_fullscreen")),
                )?],
            )?,
            &window_menu,
            &help_menu,
        ],
    )
}

pub fn is_bundled_origin(url: &url::Url) -> bool {
    url.scheme() == "tauri"
        || ((url.scheme() == "http" || url.scheme() == "https")
            && url.host_str() == Some("tauri.localhost"))
}

fn monitor_work_areas(app: &tauri::App) -> Vec<MonitorWorkArea> {
    app.available_monitors()
        .unwrap_or_default()
        .into_iter()
        .map(|monitor| {
            let work_area = monitor.work_area();
            MonitorWorkArea {
                x: work_area.position.x,
                y: work_area.position.y,
                width: work_area.size.width,
                height: work_area.size.height,
                scale_factor: monitor.scale_factor(),
            }
        })
        .collect()
}

fn capture_normal_window_state(window: &WebviewWindow) -> Result<Option<WindowState>> {
    if window.is_maximized()? {
        return Ok(None);
    }
    let scale_factor = window.scale_factor()?;
    anyhow::ensure!(
        scale_factor.is_finite() && scale_factor > 0.0,
        "main window scale factor is invalid"
    );
    let physical_position = window.outer_position()?;
    let physical_size = window.inner_size()?;
    let logical_width = f64::from(physical_size.width) / scale_factor;
    let logical_height = f64::from(physical_size.height) / scale_factor;
    Ok(Some(WindowState {
        physical_position: Some(PhysicalWindowPosition {
            x: physical_position.x,
            y: physical_position.y,
        }),
        logical_size: Some(LogicalWindowSize {
            width: logical_width,
            height: logical_height,
        }),
        physical_size: Some(PhysicalWindowSize {
            width: physical_size.width,
            height: physical_size.height,
        }),
        scale_factor: Some(scale_factor),
        maximized: false,
    }))
}

fn refresh_window_state_cache(window: &WebviewWindow, app: &tauri::AppHandle) {
    if let Ok(Some(window_state)) = capture_normal_window_state(window) {
        *app.state::<AppState>().window_state_cache.lock() = Some(window_state);
    }
}

fn persist_main_window_state(app: &tauri::AppHandle) -> Result<()> {
    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };
    let maximized = window.is_maximized()?;
    let mut window_state = if maximized {
        app.state::<AppState>()
            .window_state_cache
            .lock()
            .clone()
            .unwrap_or_default()
    } else {
        capture_normal_window_state(&window)?.unwrap_or_default()
    };
    window_state.maximized = maximized;
    app.state::<AppState>()
        .host_state
        .lock()
        .save_window_state(window_state.clone())?;
    *app.state::<AppState>().window_state_cache.lock() = Some(window_state);
    Ok(())
}

pub(crate) fn request_close(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if state.close_in_progress.swap(true, Ordering::AcqRel) {
        return;
    }
    let _ = persist_main_window_state(app);
    #[cfg(feature = "updater")]
    updater::invalidate_for_lifecycle(app);
    let operation_app = app.clone();
    tauri::async_runtime::spawn(async move {
        let worker_app = operation_app.clone();
        let should_exit = tauri::async_runtime::spawn_blocking(move || {
            let state = worker_app.state::<AppState>();
            let sidecar_is_none = state.sidecar.lock().is_none();
            if sidecar_is_none {
                matches!(
                    state.lifecycle.lock().state,
                    LifecycleState::Stopped | LifecycleState::Recovering
                )
            } else {
                sidecar::stop_with_dialog(&worker_app, "quit").is_ok()
            }
        })
        .await
        .unwrap_or(false);
        if should_exit {
            operation_app.exit(0);
        } else {
            operation_app
                .state::<AppState>()
                .close_in_progress
                .store(false, Ordering::Release);
        }
    });
}

fn build_main_window(
    app: &tauri::App,
    bootstrap_operation_id: uuid::Uuid,
    language: &str,
    theme: &str,
    startup_recovery_error: Option<&str>,
) -> tauri::Result<()> {
    let bootstrap = json!({
        "bootstrapOperationId": bootstrap_operation_id,
        "language": language,
        "theme": theme,
        "startupRecoveryError": startup_recovery_error,
    });
    let initialization_script = format!("window.__IAC_BOOTSTRAP__ = {bootstrap};");
    let saved_window_state = app
        .state::<AppState>()
        .host_state
        .lock()
        .state()
        .window_state
        .clone();
    let placement = restored_window_placement(
        saved_window_state.as_ref(),
        monitor_work_areas(app).as_slice(),
    );
    let navigation_app = app.handle().clone();
    let new_window_app = app.handle().clone();
    let mut window_builder =
        WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
            .title("iac-code")
            .inner_size(placement.logical_size.width, placement.logical_size.height)
            .min_inner_size(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
            .maximized(placement.maximized)
            .visible(true)
            .disable_drag_drop_handler()
            .initialization_script(initialization_script)
            .on_navigation(move |url| {
                let allowed = if is_bundled_origin(url) {
                    true
                } else {
                    let state = navigation_app.state::<AppState>();
                    let lifecycle = state.lifecycle.lock();
                    lifecycle.healthy_origin.as_ref().is_some_and(|origin| {
                        url.scheme() == origin.scheme()
                            && url.host_str() == origin.host_str()
                            && url.port_or_known_default() == origin.port_or_known_default()
                    })
                };
                if !allowed && matches!(url.scheme(), "http" | "https") {
                    let _ = navigation_app.opener().open_url(url.as_str(), None::<&str>);
                }
                allowed
            })
            .on_new_window(move |url, _features| {
                if matches!(url.scheme(), "http" | "https") {
                    let _ = new_window_app.opener().open_url(url.as_str(), None::<&str>);
                }
                NewWindowResponse::Deny
            });
    if let Some((x, y)) = placement.logical_position {
        window_builder = window_builder.position(x, y);
    }
    let window = window_builder.build()?;
    refresh_window_state_cache(&window, app.handle());
    let close_app = app.handle().clone();
    let event_window = window.clone();
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_)) {
            refresh_window_state_cache(&event_window, &close_app);
            return;
        }
        let WindowEvent::CloseRequested { api, .. } = event else {
            return;
        };
        api.prevent_close();
        request_close(&close_app);
    });
    Ok(())
}

#[cfg(windows)]
fn internal_update_argument(name: &str) -> Option<String> {
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        if argument == name {
            return arguments.next();
        }
    }
    None
}

#[cfg(windows)]
fn schedule_windows_update_cleanup(marker_path: PathBuf, attempt: windows_update::UpdateAttempt) {
    std::thread::spawn(move || {
        use std::time::Duration;

        let helper_exited = match (attempt.helper_pid, attempt.helper_creation_time) {
            (Some(helper_pid), Some(helper_creation_time)) => {
                windows_update::wait_for_process_identity_exit(
                    helper_pid,
                    helper_creation_time,
                    Duration::from_secs(60),
                )
            }
            _ => Err(anyhow::anyhow!(
                "completed Windows update has no helper process identity"
            )),
        };
        if !matches!(helper_exited, Ok(true)) {
            if let Ok(mut current) = windows_update::load_marker(&marker_path) {
                if current.attempt_id == attempt.attempt_id
                    && matches!(current.phase, windows_update::UpdateAttemptPhase::Complete)
                {
                    current.error = Some(match helper_exited {
                        Ok(false) => {
                            "Windows updater helper did not exit before the cleanup deadline"
                                .to_string()
                        }
                        Err(error) => {
                            format!("Windows updater helper exit could not be confirmed: {error:#}")
                        }
                        Ok(true) => unreachable!(),
                    });
                    let _ = windows_update::save_marker(&marker_path, &mut current);
                }
            }
            return;
        }
        let Ok(current) = windows_update::load_marker(&marker_path) else {
            return;
        };
        if current.attempt_id != attempt.attempt_id
            || !matches!(current.phase, windows_update::UpdateAttemptPhase::Complete)
        {
            return;
        }
        if let Some(staging) = current.helper_executable_path.parent() {
            match fs::remove_dir_all(staging) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => return,
            }
        }
        let _ = fs::remove_file(marker_path);
    });
}

#[cfg(windows)]
fn reconcile_windows_update(
    app: &tauri::App,
    paths: &HostPaths,
    language: &str,
) -> Result<Option<String>> {
    use windows_update::{load_marker, save_marker, UpdateAttemptPhase, MARKER_NAME};

    let channel_marker = paths.host_state_dir.join(MARKER_NAME);
    let legacy_marker = paths.family_root.join(MARKER_NAME);
    // An N-1 helper watches the exact legacy marker path it passed to the new Host.
    // Keep using that path for an in-flight handoff; copying it would strand the helper
    // waiting for an acknowledgement written to a different file.
    let marker_path = if channel_marker.exists() || !legacy_marker.exists() {
        channel_marker
    } else {
        legacy_marker
    };
    let mut attempt = match load_marker(&marker_path) {
        Ok(attempt) => attempt,
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|error| error.kind() == std::io::ErrorKind::NotFound) =>
        {
            return Ok(None)
        }
        Err(_error) => {
            return Ok(Some(
                desktop_text(language, "update_recovery_failed").to_string(),
            ))
        }
    };
    let handoff = internal_update_argument("--desktop-update-attempt");
    if let Some(attempt_id) = handoff {
        if attempt_id == attempt.attempt_id
            && app.package_info().version.to_string() == attempt.target_version
            && matches!(attempt.phase, UpdateAttemptPhase::HandoffPending)
        {
            attempt.phase = UpdateAttemptPhase::Complete;
            attempt.error = None;
            save_marker(&marker_path, &mut attempt)?;
            schedule_windows_update_cleanup(marker_path, attempt);
            return Ok(None);
        }
        return Ok(Some(
            desktop_text(language, "update_recovery_failed").to_string(),
        ));
    }
    if matches!(attempt.phase, UpdateAttemptPhase::Complete)
        && app.package_info().version.to_string() == attempt.target_version
    {
        schedule_windows_update_cleanup(marker_path, attempt);
        return Ok(None);
    }
    Ok(Some(
        desktop_text(language, "update_recovery_failed").to_string(),
    ))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init());

    #[cfg(feature = "updater")]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());

    let builder = builder.setup(|app| {
        let paths = HostPaths::create(app)?;
        let language = configured_language(app.handle());
        let theme = configured_theme(app.handle());
        #[cfg(windows)]
        let startup_recovery_error = reconcile_windows_update(app, &paths, &language)?;
        #[cfg(not(windows))]
        let startup_recovery_error = None;
        let desktop_install_id = sidecar::install_id_for_identifier(&app.config().identifier);
        let host_state = HostStateStore::open_with_legacy(
            &paths.host_state_dir,
            Some(&paths.family_root),
            &desktop_install_id,
        )?;
        let window_state_cache = host_state.state().window_state.clone();
        let lifecycle = LifecycleCoordinator::default();
        let bootstrap_operation_id = lifecycle.bootstrap_operation_id;
        app.set_menu(localized_menu(app.handle(), &language)?)?;
        app.manage(AppState {
            paths,
            host_state: Mutex::new(host_state),
            window_state_cache: Mutex::new(window_state_cache),
            lifecycle: Mutex::new(lifecycle),
            sidecar: Mutex::new(None),
            close_in_progress: AtomicBool::new(false),
            language: language.clone(),
            startup_recovery_error: startup_recovery_error.clone(),
            #[cfg(feature = "updater")]
            updater: updater::UpdaterCoordinator::default(),
        });
        build_main_window(
            app,
            bootstrap_operation_id,
            &language,
            &theme,
            startup_recovery_error.as_deref(),
        )?;
        Ok(())
    });

    #[cfg(feature = "updater")]
    let builder = builder.invoke_handler(tauri::generate_handler![
        commands::complete_bootstrap_check,
        commands::select_project_directory,
        commands::retry_start_sidecar,
        commands::quit_app,
        commands::confirm_secret_reveal,
        commands::restart_sidecar,
        commands::open_diagnostics_directory,
        commands::open_external_url,
        commands::check_update,
        commands::dismiss_update,
        commands::download_update,
        commands::install_update,
    ]);

    #[cfg(not(feature = "updater"))]
    let builder = builder.invoke_handler(tauri::generate_handler![
        commands::complete_bootstrap_check,
        commands::select_project_directory,
        commands::retry_start_sidecar,
        commands::quit_app,
        commands::confirm_secret_reveal,
        commands::restart_sidecar,
        commands::open_diagnostics_directory,
        commands::open_external_url,
    ]);

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building iac-code Desktop");
    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { code, api, .. } = event {
            if code.is_none() {
                api.prevent_exit();
                request_close(app);
            } else {
                let _ = persist_main_window_state(app);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_desktop_strings_cover_every_web_language() {
        for language in ["zh", "es", "fr", "de", "ja", "pt"] {
            for key in [
                "quit_title",
                "force_quit",
                "return_app",
                "wait",
                "active_work",
                "close_failed",
                "select_project",
                "reveal_secret",
                "project_persist_failed",
                "runtime_stopped",
                "runtime_restart_failed",
                "update_recovery_failed",
            ] {
                assert_ne!(desktop_text(language, key), desktop_text("en", key));
            }
            for key in [
                "menu_file",
                "menu_edit",
                "menu_view",
                "menu_window",
                "menu_help",
                "menu_about",
                "menu_services",
                "menu_hide",
                "menu_hide_others",
                "menu_quit",
                "menu_close_window",
                "menu_undo",
                "menu_redo",
                "menu_cut",
                "menu_copy",
                "menu_paste",
                "menu_select_all",
                "menu_fullscreen",
                "menu_minimize",
                "menu_maximize",
            ] {
                assert_ne!(desktop_text(language, key), "iac-code");
            }
            assert_ne!(
                desktop_text(language, "menu_file"),
                desktop_text("en", "menu_file")
            );
        }
    }

    #[test]
    fn desktop_settings_language_shape_matches_python_settings() {
        let settings: DesktopSettings =
            serde_yaml::from_str("ui:\n  language: ja\n").expect("parse Desktop settings fixture");
        assert_eq!(
            settings.ui.and_then(|ui| ui.language).as_deref(),
            Some("ja")
        );
    }

    #[test]
    fn desktop_settings_theme_shape_matches_python_settings() {
        let settings: DesktopSettings = serde_yaml::from_str("appearance:\n  theme: evergreen\n")
            .expect("parse Desktop appearance fixture");
        assert_eq!(
            settings
                .appearance
                .and_then(|appearance| appearance.theme)
                .as_deref(),
            Some("evergreen")
        );
        assert_eq!(supported_theme("sepia"), Some("sepia"));
        assert_eq!(supported_theme("electric-blue"), None);
    }

    #[test]
    fn desktop_system_language_normalizes_supported_locale_shapes() {
        assert_eq!(supported_language("zh_CN.UTF-8"), Some("zh"));
        assert_eq!(supported_language("pt-BR"), Some("pt"));
        assert_eq!(supported_language("ja_JP.UTF-8@calendar"), Some("ja"));
        assert_eq!(supported_language("C.UTF-8"), None);
    }

    #[test]
    fn desktop_system_language_uses_first_supported_environment_value() {
        assert_eq!(
            language_from_values(["C.UTF-8", "zh_CN.UTF-8", "en_US.UTF-8"]),
            Some("zh".to_string())
        );
        assert_eq!(language_from_values(["C", "POSIX"]), None);
    }

    #[test]
    fn host_paths_isolate_channel_state_and_share_family_install_locks() {
        let family_root = PathBuf::from("/desktop-family");
        let appimage = HostPaths::from_family_root(family_root.clone(), "appimage");
        let deb = HostPaths::from_family_root(family_root.clone(), "deb");

        assert_eq!(appimage.host_state_dir, family_root.join("appimage"));
        assert_eq!(appimage.runtime_dir, family_root.join("appimage/runtime"));
        assert_eq!(deb.host_state_dir, family_root.join("deb"));
        assert_eq!(
            appimage.install_lock_dir,
            family_root.join("iac-code-desktop-install-locks")
        );
        assert_eq!(appimage.install_lock_dir, deb.install_lock_dir);
    }
}
