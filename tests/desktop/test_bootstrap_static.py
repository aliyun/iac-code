from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_recovery_bootstrap_uses_navigation_scoped_operation() -> None:
    source = (ROOT / "desktop/bootstrap/index.html").read_text(encoding="utf-8")

    assert 'parameters.get("bootstrapOperationId") || bootstrap.bootstrapOperationId' in source
    assert 'if (mode === "recovery") {' in source
    assert source.index('await invoke("complete_bootstrap_check"') < source.index('if (mode === "recovery") {')
    assert "project.hidden = false;" in source
    assert source.count("stopping:") == 7


def test_bootstrap_uses_product_tagline_instead_of_webview_check_status() -> None:
    source = (ROOT / "desktop/bootstrap/index.html").read_text(encoding="utf-8")

    assert "描述一个任务、命令或基础设施变更，交给 IaC Code。" in source
    assert "正在检查系统 WebView。" not in source
    assert source.count("tagline:") == 7
    assert "status.textContent = text.tagline;" in source


def test_bootstrap_localizes_recovery_errors_before_becoming_visible() -> None:
    source = (ROOT / "desktop/bootstrap/index.html").read_text(encoding="utf-8")

    for key in ("bridge", "projectError", "runtimeError", "diagnosticsError", "portBusy"):
        assert source.count(f"{key}:") == 7
    assert "visibility: hidden" in source
    assert 'document.documentElement.lang = bootstrap.language || "en";' in source
    assert 'document.body.style.visibility = "visible";' in source
    assert "status.textContent = String(error);" not in source
    assert 'throw new Error("The native Desktop bridge is unavailable.");' not in source


def test_bootstrap_does_not_flash_project_picker_during_automatic_start() -> None:
    source = (ROOT / "desktop/bootstrap/index.html").read_text(encoding="utf-8")
    commands = (ROOT / "desktop/src-tauri/src/commands.rs").read_text(encoding="utf-8")

    assert 'const runtimeStarted = await invoke("complete_bootstrap_check"' in source
    assert "if (runtimeStarted) {\n            return;\n          }" in source
    assert source.index("if (runtimeStarted) {") < source.index("title.textContent = text.choose;")
    assert ") -> Result<bool, String> {" in commands
    assert "Ok(true)" in commands


def test_bootstrap_logo_and_colors_follow_the_configured_theme() -> None:
    source = (ROOT / "desktop/bootstrap/index.html").read_text(encoding="utf-8")
    logo = (ROOT / "desktop/bootstrap/iac-code-logo.svg").read_text(encoding="utf-8")

    assert '<svg class="mark" viewBox="0 0 1024 1024"' in source
    assert '>IaC</div>' not in source
    assert 'bootstrap.theme || "graphite"' in source
    assert 'document.documentElement.dataset.theme = theme;' in source
    for theme in ("midnight", "evergreen", "sepia", "ivory"):
        assert f':root[data-theme="{theme}"]' in source
    assert "background: var(--desktop-bg)" in source
    assert "background: #2563eb" not in source
    assert 'viewBox="0 0 1024 1024"' in logo
    assert logo.count("linearGradient") == 4
    assert "#45b4ff" in source
    assert "#45b4ff" in logo
    assert "#e778cb" in source
    assert "#e778cb" in logo
    assert "M464 128H320" in source
    assert "M464 128H320" in logo
    assert source.count('stroke-width="80"') == 1
    assert logo.count('stroke-width="80"') == 1
    assert '<circle cx="512" cy="518" r="35"' in source
    assert '<circle cx="512" cy="518" r="35"' in logo


def test_bundled_navigation_uses_platform_correct_origin_and_rotates_operation() -> None:
    source = (ROOT / "desktop/src-tauri/src/sidecar.rs").read_text(encoding="utf-8")

    assert 'let bundled_base = "http://tauri.localhost/index.html";' in source
    assert 'let bundled_base = "tauri://localhost/index.html";' in source
    assert "state.lifecycle.lock().begin_bootstrap()" in source
    assert 'query.append_pair("bootstrapOperationId", &bootstrap_operation_id);' in source
    assert 'query.append_pair("theme", &crate::configured_theme(app));' in source
