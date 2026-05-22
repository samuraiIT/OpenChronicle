#!/usr/bin/env python3
"""linux-atspi-watcher — AT-SPI event monitor for Linux.

Replaces mac-ax-watcher.swift. Writes JSONL to stdout matching the exact
event contract expected by watcher.py:_read_events().

Usage: OPENCHRONICLE_AX_WATCHER=./resources/linux-atspi-watcher openchronicle start

Requirements: python3-pyatspi, at-spi2-core, dbus-python.
All installed on server (python3-pyatspi 2.46.1, at-spi2-core).
"""

import json
import os
import re
import signal
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import pyatspi
import dbus
import dbus.mainloop.glib
from gi.repository import GLib  # type: ignore[import-untyped]

# ── Debounce typing config ──────────────────────────────────────────────
TYPING_DEBOUNCE_SEC = float(os.environ.get("OC_TYPING_DEBOUNCE_SEC", "0.5"))
MIN_CAPTURE_GAP_SEC = float(os.environ.get("OC_MIN_CAPTURE_GAP_SEC", "1.0"))

# ── Browser detection ───────────────────────────────────────────────────
_BROWSER_NAMES = {
    "firefox", "google-chrome", "chromium", "brave-browser",
    "microsoft-edge", "opera", "vivaldi", "chromium-browser",
}
_BROWSER_SUBSTRINGS = [
    "firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi",
]


def _is_browser(app_name: str) -> bool:
    name_lower = app_name.lower()
    for s in _BROWSER_SUBSTRINGS:
        if s in name_lower:
            return True
    return False


def _get_browser_url(window_pid: int) -> Optional[str]:
    """Attempt to extract browser URL via xdotool/xprop.

    Limited best-effort — many modern browsers sandbox their UI from AT-SPI.
    Returns None when extraction fails.
    """
    try:
        # Try getting active window and its URL via xdotool
        import subprocess
        wid = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=1
        ).stdout.strip()
        if wid:
            # xprop _NET_WM_NAME gives window title which may contain URL in browser
            title = subprocess.run(
                ["xprop", "-id", wid, "_NET_WM_NAME"],
                capture_output=True, text=True, timeout=1
            ).stdout.strip()
            if title:
                # Extract URL-like pattern from window title
                import re
                m = re.search(r'https?://\S+|www\.\S+', title)
                if m:
                    return m.group(0)
    except Exception:
        pass
    return None


# ── ATSPIWatcher ────────────────────────────────────────────────────────

class ATSPIWatcher:
    """Monitors AT-SPI events and emits JSONL matching macOS watcher contract."""

    def __init__(self) -> None:
        self._typing_timer: Optional[threading.Timer] = None
        self._typing_lock = threading.Lock()
        self._pending_text: str = ""
        self._pending_app_name: str = ""
        self._pending_window_title: str = ""
        self._pending_bundle_id: str = ""
        self._pending_pid: int = 0
        self._last_capture_ts: float = 0.0
        self._dbus_bus = dbus.SessionBus()

    # ── helpers ──────────────────────────────────────────────────────

    def _emit(self, event_type: str, **kwargs: Any) -> None:
        now = time.monotonic()
        gap = now - self._last_capture_ts
        # Rate-limit non-focus events
        if event_type not in ("AXFocusedWindowChanged", "AXApplicationActivated"):
            if gap < MIN_CAPTURE_GAP_SEC:
                return
        self._last_capture_ts = now

        event: dict[str, Any] = {
            "event_type": event_type,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **kwargs,
        }
        print(json.dumps(event, ensure_ascii=False), flush=True)

    @staticmethod
    def _bundle_id(app_name: str) -> str:
        """Convert app name to bundle_id-like format: 'Code - OSS' → 'com.code.oss'"""
        name = app_name.lower().strip()
        name = re.sub(r'[^a-z0-9]+', '.', name)
        name = name.strip('.')
        if not name:
            name = "unknown"
        return f"com.{name}"

    def _get_focused_app_info(self) -> tuple[str, str, str, int]:
        """Get focused app: (app_name, window_title, bundle_id, pid) via D-Bus + xdotool."""
        app_name = "Unknown"
        window_title = "(untitled)"
        pid = 0

        # Try D-Bus approach first for Wayland/X11
        try:
            desktop = pyatspi.Registry.getDesktop(0)
            for i in range(desktop.childCount):
                app = desktop.getChildAt(i)
                app_states = app.getState()
                if app_states.contains(pyatspi.STATE_ACTIVE):
                    app_name = app.name or "Unknown"
                    pid = app.getProcessId() or 0
                    # Get focused window
                    for j in range(app.childCount):
                        win = app.getChildAt(j)
                        win_states = win.getState()
                        if win_states.contains(pyatspi.STATE_FOCUSED) or win_states.contains(pyatspi.STATE_SELECTED):
                            window_title = win.name or "(untitled)"
                            break
                    break
        except Exception:
            pass

        # Fallback to xdotool if AT-SPI didn't find active window
        if app_name == "Unknown":
            try:
                import subprocess
                result = subprocess.run(
                    ["xdotool", "getactivewindow", "getwindowname"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    window_title = result.stdout.strip() or "(untitled)"
                    # Get PID
                    wid = subprocess.run(
                        ["xdotool", "getactivewindow"],
                        capture_output=True, text=True, timeout=1
                    ).stdout.strip()
                    if wid:
                        pid_result = subprocess.run(
                            ["xdotool", "getwindowpid", wid],
                            capture_output=True, text=True, timeout=1
                        )
                        if pid_result.returncode == 0:
                            pid = int(pid_result.stdout.strip())
                        # Get process name from PID
                        name_result = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "comm="],
                            capture_output=True, text=True, timeout=1
                        )
                        if name_result.returncode == 0:
                            app_name = name_result.stdout.strip()
            except Exception:
                pass

        bundle_id = self._bundle_id(app_name)
        return app_name, window_title, bundle_id, pid

    # ── event handlers ───────────────────────────────────────────────

    def _on_window_activate(self, event: Any) -> None:
        """window:activate -> AXFocusedWindowChanged + AXApplicationActivated"""
        try:
            app_name, window_title, bundle_id, pid = self._get_focused_app_info()

            # AXApplicationActivated (app-level focus)
            self._emit("AXApplicationActivated",
                       app_name=app_name,
                       bundle_id=bundle_id,
                       pid=pid)

            # AXFocusedWindowChanged (window-level focus)
            self._emit("AXFocusedWindowChanged",
                       app_name=app_name,
                       bundle_id=bundle_id,
                       window_title=window_title,
                       pid=pid)

            # Browser URL extraction
            if _is_browser(app_name):
                url = _get_browser_url(pid)
                if url:
                    self._emit("AXFocusedWindowChanged",
                               app_name=app_name,
                               bundle_id=bundle_id,
                               window_title=window_title,
                               pid=pid,
                               url=url)

        except Exception as exc:
            print(json.dumps({
                "event_type": "_error",
                "message": f"window_activate handler: {exc}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }), flush=True)

    def _on_text_insert(self, event: Any) -> None:
        """object:text-changed:insert -> debounced UserTextInput"""
        try:
            source = event.source
            # Extract inserted text
            text = ""
            try:
                # event.detail1 = offset, event.any_data = inserted text
                text = event.any_data
            except AttributeError:
                try:
                    text = source.getText(event.detail1, event.detail1 + 1) if hasattr(source, 'getText') else ""
                except Exception:
                    pass

            if not text or not text.strip():
                return

            app_name, window_title, bundle_id, pid = self._get_focused_app_info()

            with self._typing_lock:
                self._pending_text += text
                self._pending_app_name = app_name
                self._pending_window_title = window_title
                self._pending_bundle_id = bundle_id
                self._pending_pid = pid

                # Reset debounce timer
                if self._typing_timer is not None:
                    self._typing_timer.cancel()
                self._typing_timer = threading.Timer(
                    TYPING_DEBOUNCE_SEC, self._flush_typing
                )
                self._typing_timer.daemon = True
                self._typing_timer.start()

        except Exception as exc:
            print(json.dumps({
                "event_type": "_error",
                "message": f"text_insert handler: {exc}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }), flush=True)

    def _flush_typing(self) -> None:
        """Emit UserTextInput after debounce period."""
        with self._typing_lock:
            text = self._pending_text
            if not text:
                return
            app_name = self._pending_app_name
            window_title = self._pending_window_title
            bundle_id = self._pending_bundle_id
            pid = self._pending_pid

            self._pending_text = ""
            self._typing_timer = None

        self._emit("UserTextInput",
                   app_name=app_name,
                   bundle_id=bundle_id,
                   window_title=window_title,
                   pid=pid,
                   text=text[:500])  # Truncate long pastes

    def _on_value_changed(self, event: Any) -> None:
        """object:value-changed -> AXValueChanged (debounced by event_dispatcher)"""
        try:
            app_name, window_title, bundle_id, pid = self._get_focused_app_info()
            self._emit("AXValueChanged",
                       app_name=app_name,
                       bundle_id=bundle_id,
                       window_title=window_title,
                       pid=pid)
        except Exception as exc:
            print(json.dumps({
                "event_type": "_error",
                "message": f"value_changed handler: {exc}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }), flush=True)

    def _on_state_changed(self, event: Any) -> None:
        """object:state-changed:focused -> UserMouseClick (heuristic)"""
        try:
            source = event.source
            role = source.getRoleName() if hasattr(source, 'getRoleName') else ""
            # Only emit for interactive elements (buttons, links, etc.)
            if role in ("push button", "toggle button", "radio button",
                         "check box", "link", "menu item", "list item",
                         "tree item", "tab", "combo box"):
                app_name, window_title, bundle_id, pid = self._get_focused_app_info()
                self._emit("UserMouseClick",
                           app_name=app_name,
                           bundle_id=bundle_id,
                           window_title=window_title,
                           pid=pid)
        except Exception:
            pass  # MouseClick is best-effort; silence errors

    def _on_property_changed(self, event: Any) -> None:
        """object:property-change:accessible-name -> AXTitleChanged (skip by dispatcher)"""
        try:
            app_name, window_title, bundle_id, pid = self._get_focused_app_info()
            self._emit("AXTitleChanged",
                       app_name=app_name,
                       bundle_id=bundle_id,
                       window_title=window_title,
                       pid=pid)
        except Exception:
            pass  # TitleChanged is noisy; dispatcher already skips it

    # ── main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        """Register event listeners and start the GLib main loop."""
        import gi
        gi.require_version('GLib', '2.0')

        # Initialize D-Bus main loop for AT-SPI
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        registry = pyatspi.Registry

        # Window/application focus changes
        registry.registerEventListener(self._on_window_activate, 'window:activate')
        registry.registerEventListener(self._on_window_activate, 'window:deactivate')

        # Text input
        registry.registerEventListener(self._on_text_insert, 'object:text-changed:insert')

        # Value changes
        registry.registerEventListener(self._on_value_changed, 'object:value-changed')

        # State changes (mouse clicks heuristic)
        registry.registerEventListener(self._on_state_changed, 'object:state-changed:focused')

        # Title changes
        registry.registerEventListener(self._on_property_changed, 'object:property-change:accessible-name')

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

        # Emit startup heartbeat
        self._emit("_startup", version="1.0.0-linux", platform="linux")

        # Run GLib event loop
        try:
            loop = GLib.MainLoop()
            loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            sys.exit(0)


if __name__ == "__main__":
    ATSPIWatcher().run()
