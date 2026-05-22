#!/usr/bin/env python3
"""linux-ax-helper — one-shot AT-SPI tree dump for Linux.

Replaces mac-ax-helper.swift. Called by ax_capture.py:capture_once().
Dumps full AT-SPI accessibility tree as JSON to stdout.

Expected output format (matches mac-ax-helper.swift output parsed by ax_capture.py):
{
  "apps": [
    {
      "name": "App Name",
      "bundle_id": "com.example.app",
      "pid": 12345,
      "is_frontmost": true,
      "windows": [
        {
          "title": "Window Title",
          "focused": true,
          "elements": [{"role": "...", "value": "...", ...}]
        }
      ]
    }
  ]
}

Requirements: python3-pyatspi (installed, 2.46.1).
"""

import json
import re
import sys
import time
from typing import Any, Optional

import pyatspi

# Maximum depth for element tree recursion
MAX_DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 100

# Maximum characters for element values (prevent huge dumps)
MAX_VALUE_CHARS = 5000


def _bundle_id(app_name: str) -> str:
    """Convert app name to bundle_id-like format."""
    name = app_name.lower().strip()
    name = re.sub(r'[^a-z0-9]+', '.', name)
    name = name.strip('.')
    if not name:
        name = "unknown"
    return f"com.{name}"


def _is_frontmost(accessible: Any) -> bool:
    """Check if accessible is the frontmost/focused application."""
    try:
        states = accessible.getState()
        return states.contains(pyatspi.STATE_ACTIVE) or states.contains(pyatspi.STATE_FOCUSED)
    except Exception:
        return False


def _get_element_role(accessible: Any) -> str:
    """Get role name of accessible element."""
    try:
        return accessible.getRoleName() or ""
    except Exception:
        return "unknown"


def _get_element_value(accessible: Any) -> Optional[str]:
    """Get text value/content of accessible element."""
    try:
        # Try name first
        name = accessible.name
        if name and len(name) > 1:
            return name[:MAX_VALUE_CHARS]

        # Try text interface
        text_iface = accessible.queryText()
        if text_iface:
            char_count = text_iface.characterCount
            if char_count > 0:
                return text_iface.getText(0, min(char_count, MAX_VALUE_CHARS))

        # Try description
        desc = accessible.description
        if desc:
            return desc[:MAX_VALUE_CHARS]
    except Exception:
        pass
    return None


def _dump_element(accessible: Any, depth: int = 0) -> Optional[dict[str, Any]]:
    """Recursively dump an accessibility element."""
    if depth >= MAX_DEPTH:
        return None

    try:
        role = _get_element_role(accessible)
        value = _get_element_value(accessible)

        # Skip container-only nodes if no value and no interesting children
        children = []
        try:
            child_count = accessible.childCount
            for i in range(min(child_count, 50)):  # Limit children for performance
                child = accessible.getChildAt(i)
                child_elem = _dump_element(child, depth + 1)
                if child_elem:
                    children.append(child_elem)
        except Exception:
            pass

        # Only include if has value or has children
        if value or children:
            elem: dict[str, Any] = {"role": role}
            if value:
                elem["value"] = value
            if children:
                elem["children"] = children
            return elem

    except Exception:
        pass
    return None


def dump_tree() -> None:
    """Main entry point: dump full AT-SPI tree as JSON."""
    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception as exc:
        error = {"error": f"Failed to access AT-SPI desktop: {exc}"}
        print(json.dumps(error, ensure_ascii=False))
        sys.exit(1)

    apps: list[dict[str, Any]] = []
    app_count = desktop.childCount

    for i in range(app_count):
        try:
            app = desktop.getChildAt(i)
            app_name = app.name or "(unnamed)"
            bundle_id = _bundle_id(app_name)
            pid = app.getProcessId() or 0
            is_frontmost = _is_frontmost(app)

            windows: list[dict[str, Any]] = []
            for j in range(app.childCount):
                try:
                    win = app.getChildAt(j)
                    title = win.name or "(untitled)"
                    focused = False
                    try:
                        focused = win.getState().contains(pyatspi.STATE_FOCUSED)
                    except Exception:
                        pass

                    elements: list[dict[str, Any]] = []
                    for k in range(min(win.childCount, 100)):
                        try:
                            child = win.getChildAt(k)
                            elem = _dump_element(child, 1)
                            if elem:
                                elements.append(elem)
                        except Exception:
                            pass

                    windows.append({
                        "title": title,
                        "focused": focused,
                        "elements": elements,
                    })
                except Exception:
                    pass

            apps.append({
                "name": app_name,
                "bundle_id": bundle_id,
                "pid": pid,
                "is_frontmost": is_frontmost,
                "windows": windows,
            })
        except Exception:
            pass

    result = {"apps": apps}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    dump_tree()
