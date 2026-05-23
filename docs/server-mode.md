# Server Mode (Headless Linux)

OpenChronicle's **server mode** extends capture to headless Linux servers — environments
without a desktop session, display, or AT-SPI applications. Instead of screen-level
events, it captures server-specific activity: git commits, agent sessions, deployments,
file changes, and system metrics.

**Status:** ✅ Complete (v20.91, May 2026)

## Architecture

```
server-events-watcher.py  ──→  event_dispatcher.py  ──→  scheduler.py
(git, agents, system)         (IMMEDIATE_EVENTS)        (trigger fallback)
          │                              │                        │
          │ JSONL stdout                  │ trigger dict           │ window_meta fallback
          ▼                              ▼                        ▼
     capture buffer  ←────────  S1 parser  ←────────  timeline blocks
```

## Quick Start

```bash
OC_WATCH_DIR=/path/to/workspace \
  OPENCHRONICLE_AX_WATCHER=./resources/server-events-watcher.py \
  openchronicle start
```

## Captured Events

| Event Type | Source | Frequency | Payload |
|---|---|---|---|
| `git_commit` | `git log` polling | Every poll cycle (10s) | hash, author, message, branch |
| `agent_activity` | Agent session files | Every poll cycle | agent_name, session_id, command |
| `system_metric` | `/proc` / `psutil` | Every 5 min | cpu%, mem%, disk_free, load_avg |
| `deployment` | Version files / releases | Every poll cycle | version, component, timestamp |
| `file_change` | `inotify` / polling | Real-time | path, change_type, size |

## Required Patches

These patches are already applied in the `linux-server-port` branch:

### `event_dispatcher.py`

Server events must be in `_IMMEDIATE_EVENTS` to bypass the debounce window.
Without this, all server events are silently dropped on the floor.

```python
_IMMEDIATE_EVENTS = {
    "AXFocusedWindowChanged", "AXApplicationActivated",
    "UserMouseClick", "UserTextInput",
    # Server-mode events
    "git_commit", "deployment", "agent_activity",
    "file_change", "system_metric",
}
```

The trigger dict must carry rich metadata so downstream (timeline, classifier)
can make sense of server events:

```python
trigger = {
    "event_type": event_type,
    "app_name": raw.get("app_name", bundle_id),
    "bundle_id": bundle_id,
    "window_title": window_title,
    "text": raw.get("text", ""),
    "commit_hash": raw.get("commit_hash", ""),
    "author": raw.get("author", ""),
}
```

### `scheduler.py`

On headless servers, `window_meta.active_window()` returns empty values
(no display → no active window). Fallback to trigger data:

```python
if trigger and (not meta.app_name or meta.app_name == ""):
    meta.app_name = trigger.get("app_name", "") or trigger.get("window_title", "") or ""
    meta.title = trigger.get("window_title", "") or trigger.get("text", "") or ""
    meta.bundle_id = trigger.get("bundle_id", "") or ""
```

Without this fallback, all captures have empty `app_name`/`title` and become
useless for timeline classification and search.

## `server-events-watcher.py`

A Python script that replaces `linux-atspi-watcher.py` on headless servers.
Emits JSONL matching the macOS/AT-SPI event contract.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `OC_WATCH_DIR` | Current directory | Git repo to watch |
| `OC_HEARTBEAT_SEC` | 300 | System metric interval |
| `OC_POLL_SEC` | 10 | Git/activity poll interval |

**Usage:**

```bash
OPENCHRONICLE_AX_WATCHER=./resources/server-events-watcher.py openchronicle start
```

## Configuration

```toml
[capture]
event_driven = true
heartbeat_minutes = 5
include_screenshot = false     # ← Critical: headless has no display
ax_timeout = 10
```

## Daemon Health Check

```bash
openchronicle status | grep -E "Health|Model|Buffer"
# Expect: Health: OK, Model: default ✓, Buffer: N captures pending
```

## Limitations

- **No visual context** — `include_screenshot` must be `false`
- **No app-level context** — `active_window()` returns empty; fallback to trigger data
- **Fewer event types** — only server-relevant events (no mouse clicks, UI interactions)
- **Coarser granularity** — poll-based (10s) vs AT-SPI push (sub-second)

## See Also

- [linux-port.md](linux-port.md) — Desktop Linux port with AT-SPI
- [hybrid-memory.md](hybrid-memory.md) — Dual-layer memory model
- [hermetic-mcp.md](hermetic-mcp.md) — Unified MCP server
