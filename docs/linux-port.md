# Linux Port (AT-SPI)

OpenChronicle's original macOS backend (`mac-ax-watcher.swift`, `mac-ax-helper.swift`) has been
ported to Linux using the AT-SPI accessibility framework. The port is feature-complete for
desktop Linux sessions and extends the architecture to support headless servers via
[server-mode](server-mode.md).

**Status:** ✅ Complete (v20.89, May 2026)

## AT-SPI Event Mapping

| macOS AX Event | Linux AT-SPI Event | Notes |
|---|---|---|
| `AXFocusedWindowChanged` | `window:activate` | App-level focus tracking |
| `AXApplicationActivated` | `window:activate` (app-level) | — |
| `UserTextInput` | `object:text-changed:insert` | With 500ms debounce for typing bursts |
| `AXValueChanged` | `object:value-changed` | — |
| `UserMouseClick` | `object:state-changed:focused` + role detection | Partial — role-based heuristic |
| `AXTitleChanged` | `object:property-change:accessible-name` | — |

## Dependencies

```bash
sudo apt install at-spi2-core python3-pyatspi
```

## Components

### `linux-atspi-watcher.py`

AT-SPI event monitor. Writes JSONL to stdout with the **same contract** as the macOS
watcher, so OpenChronicle's Python dispatcher processes events identically:

```json
{"event_type": "AXFocusedWindowChanged", "timestamp": "2026-05-22T10:30:00Z",
 "bundle_id": "com.example", "window_title": "My Window", "pid": 12345,
 "app_name": "Example App"}
```

**Design decisions:**
- `_startup` heartbeat for initialization verification
- Typing debounce: 500ms idle window → flush accumulated `UserTextInput`
- Rate limiting: `MIN_CAPTURE_GAP_SEC` (1s) for non-focus events
- Browser URL extraction: best-effort via `xdotool` + `xprop`
- Window metadata: AT-SPI primary source, `xdotool` fallback
- Exponential reconnect on GLib main loop crashes

### `linux-ax-helper.py`

One-shot AT-SPI tree dump. CLI: `python3 linux-ax-helper.py [depth]`. Output:

```json
{"apps": [{"name": "Terminal", "bundle_id": "org.gnome.Terminal", "pid": 5678,
           "is_frontmost": true, "windows": [{"title": "user@host:~",
           "focused": true, "elements": [{"role": "terminal",
           "value": "..."}]}]}],
 "platform": "linux", "timestamp": "..."}
```

**Safety limits:**
- Max recursion depth: 100 (configurable via `MAX_DEPTH`)
- Max value chars: 5000 (`MAX_VALUE_CHARS`)
- Max children: 50 per element, 100 per window
- Container-only nodes skipped

## Integration Points (3 changes in 2 files)

### 1. `watcher.py:_resolve_watcher_path()`
- Removed `platform.system() != "Darwin"` guard
- Added search for `linux-atspi-watcher` (`.py` and binary)
- `.py` files are `chmod +x` on first run
- Search order: `_bundled/` → `resources/` → `OPENCHRONICLE_AX_WATCHER` env var

### 2. `ax_capture.py:_resolve_helper_path()`
- Same pattern: removed macOS-only check, added `linux-ax-helper`

### 3. `ax_capture.py:create_provider()`
- Three branches: Darwin → `MacAXHelperProvider`, Linux → `LinuxAXHelperProvider`,
  else → `UnavailableAXProvider`
- `LinuxAXHelperProvider`: calls `/usr/bin/python3 resources/linux-ax-helper.py [depth]`
- Metadata: `platform: "linux"`, `mode: "tree-dump"`

## Limitations (Linux vs macOS)

| Feature | macOS | Linux | Status |
|---|---|---|---|
| Focus tracking | `AXFocusedWindowChanged` | `window:activate` | ✅ |
| Text input capture | `AXValueChanged` | `object:text-changed:insert` + debounce | ✅ |
| Mouse click detection | `AXPress/Release` | `state-changed:focused` + role | ⚠️ Partial |
| Screenshots | `CGWindowListCreateImage` | `mss` (cross-platform) | ✅ |
| Browser URL | AppleScript | `xdotool` + `xprop` (best-effort) | ⚠️ |
| Window metadata | `CGWindowListCopyWindowInfo` | `xdotool getactivewindow` | ✅ |
| **Headless server** | N/A | [server-mode](server-mode.md) | ✅ New |

## Smoke Test Results (May 2026)

| Test | Result |
|---|---|
| `linux-ax-helper.py 5` (SSH, headless) | `{"apps": []}` ✅ Empty tree expected |
| `linux-atspi-watcher.py` (timeout 3s) | `{"event_type": "_startup", ...}` ✅ |
| `openchronicle start --capture-only` | `AX capture initialized (Linux)` ✅ |
| Capture buffer | `schema_version: 2`, `platform: "linux"` ✅ |

## Pitfalls

1. **Watcher exits code 1 on SSH** — expected. AT-SPI requires a D-Bus session bus
   connected to a display. Use [server-mode](server-mode.md) for headless servers.
2. **Screenshots fail without `$DISPLAY`** — `mss` requires X11/Wayland.
   Set `include_screenshot = false` in `config.toml` for headless deployments.
3. **Helper returns `{"apps": []}` on SSH** — no applications in AT-SPI tree without
   a desktop session.
4. **`LinuxAXHelperProvider` has a simpler CLI** than macOS: only `[depth]` argument.
   App/visibility filters are ignored (full tree dump always).
