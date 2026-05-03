"""Desktop notification delivery for the /notify slash command.

All functions are fail-safe — notification errors are logged but never
propagate to the agent loop.

Cross-platform: Linux (notify-send), macOS (osascript),
Windows (PowerShell), and WSL (bridges to Windows via powershell.exe,
preferring notify-send via WSLg when available).
"""

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


# ---------------------------------------------------------------------------
# WSL detection
# ---------------------------------------------------------------------------

_WSL_CACHE: Optional[bool] = None


def _is_wsl() -> bool:
    """Return True when running under Windows Subsystem for Linux."""
    global _WSL_CACHE
    if _WSL_CACHE is not None:
        return _WSL_CACHE
    try:
        with open("/proc/version", "r") as f:
            _WSL_CACHE = "microsoft" in f.read().lower()
    except Exception:
        _WSL_CACHE = False
    return _WSL_CACHE


# ---------------------------------------------------------------------------
# Sentinel file
# ---------------------------------------------------------------------------

def get_notify_sentinel_path() -> Path:
    return _hermes_home() / ".notify_pending"


def set_notify_flag() -> bool:
    """Write the sentinel file to signal a pending notification."""
    try:
        p = get_notify_sentinel_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return True
    except Exception as e:
        logger.warning("Failed to write notify sentinel: %s", e)
        return False


def clear_notify_flag() -> bool:
    """Remove the sentinel file (cancel or consume notification)."""
    try:
        p = get_notify_sentinel_path()
        if not p.exists():
            return False
        p.unlink()
        return True
    except Exception as e:
        logger.warning("Failed to clear notify sentinel: %s", e)
        return False


def is_notify_pending() -> bool:
    """Check if a notification is pending."""
    return get_notify_sentinel_path().exists()


# ---------------------------------------------------------------------------
# Desktop notification
# ---------------------------------------------------------------------------

def _notify_send_available() -> bool:
    """Return True if notify-send is available and D-Bus is reachable."""
    if not shutil.which("notify-send"):
        return False
    # Quick smoke-test: verify D-Bus notification service exists
    try:
        result = subprocess.run(
            ["notify-send", "--version"],
            timeout=3, capture_output=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _show_notification_linux(title: str, message: str) -> None:
    """Desktop notification on native Linux via notify-send."""
    try:
        subprocess.run(
            ["notify-send", title, message],
            timeout=5, capture_output=True,
        )
        logger.debug("notify: Linux notification sent via notify-send")
    except FileNotFoundError:
        logger.debug("notify: notify-send not found on Linux")
    except subprocess.TimeoutExpired:
        logger.debug("notify: notify-send timed out on Linux")


def _ps_single_quote(value: str) -> str:
    """Quote a string for a single-quoted PowerShell literal."""
    return "'" + value.replace("'", "''") + "'"


def _show_notification_wsl(title: str, message: str) -> None:
    """Desktop notification in WSL via Windows balloon tip (PowerShell)."""
    logger.debug("notify: attempting WSL notification via PowerShell")
    try:
        ps_code = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            f"$n.BalloonTipTitle = {_ps_single_quote(title)}; "
            f"$n.BalloonTipText = {_ps_single_quote(message)}; "
            "$n.Visible = $true; "
            "$n.ShowBalloonTip(3000); "
            "[System.Windows.Forms.Application]::DoEvents(); "
            "Start-Sleep -Seconds 4; "
            "$n.Dispose()"
        )
        result = subprocess.run(
            ["powershell.exe", "-c", ps_code],
            timeout=8, capture_output=True,
        )
        if result.returncode != 0:
            logger.debug("notify: PowerShell balloon failed (rc=%d, stderr=%s)",
                         result.returncode, result.stderr.decode(errors="replace")[:200])
        else:
            logger.debug("notify: WSL notification sent via PowerShell")
    except subprocess.TimeoutExpired:
        logger.debug("notify: PowerShell balloon timed out")
    except FileNotFoundError:
        logger.debug("notify: powershell.exe not found — is WSL properly configured?")
    except Exception as e:
        logger.warning("WSL notification failed: %s", e)


def _show_desktop_notification(title: str, message: str) -> None:
    """Show a desktop notification bubble.

    WSL path: prefer notify-send via WSLg (native Windows toasts),
    fall back to PowerShell balloon tip.
    """
    try:
        if _is_wsl():
            # WSLg path: notify-send bridges to native Windows notifications
            if _notify_send_available():
                logger.debug("notify: WSLg notify-send available, using D-Bus path")
                _show_notification_linux(title, message)
                return
            logger.debug("notify: notify-send not available in WSL, falling back to PowerShell")
            _show_notification_wsl(title, message)
        elif _SYSTEM == "Linux":
            _show_notification_linux(title, message)
        elif _SYSTEM == "Darwin":
            escaped_title = title.replace('\\', '\\\\').replace('"', '\\"')
            escaped_message = message.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f"display notification \"{escaped_message}\" with title \"{escaped_title}\""],
                timeout=5, capture_output=True,
            )
            logger.debug("notify: macOS notification sent via osascript")
        elif _SYSTEM == "Windows":
            ps_code = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                f"$n.BalloonTipTitle = {_ps_single_quote(title)}; "
                f"$n.BalloonTipText = {_ps_single_quote(message)}; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(3000); "
                "Start-Sleep -Seconds 4"
            )
            subprocess.run(
                ["powershell", "-c", ps_code],
                timeout=8, capture_output=True,
            )
            logger.debug("notify: Windows notification sent via PowerShell")
    except Exception as e:
        logger.debug("Desktop notification failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fire_notification(
    config: Optional[dict] = None,
    *,
    title: str = "Hermes Agent",
    message: str = "Task complete",
) -> None:
    """Fire a desktop notification.

    All errors are caught silently — notification failure must never
    crash the idle loop.

    Args:
        config: Optional config dict.  Reads from config.yaml when None.
        title: Desktop notification title.
        message: Desktop notification body.
    """
    _show_desktop_notification(title, message)


def fire_approval_request_notification() -> None:
    """Notify that Hermes is blocked waiting for command approval.

    This intentionally does not clear the /notify sentinel; the final
    turn-complete notification should still fire after the user responds.
    """
    fire_notification(message="Input needed: approval required")