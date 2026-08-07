"""Desktop-only runtime support for the Tauri sidecar distribution."""

from iac_code.desktop.runtime import DesktopInstallContext, DesktopRuntimeConfig

DESKTOP_PROTOCOL_VERSION = 1

__all__ = ["DESKTOP_PROTOCOL_VERSION", "DesktopInstallContext", "DesktopRuntimeConfig"]
