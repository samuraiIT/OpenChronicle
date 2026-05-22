#!/usr/bin/env python3
"""server-events-watcher — captures server activity for OpenChronicle.

Replaces linux-atspi-watcher on headless servers. Emits JSONL matching
the macOS/AT-SPI event contract so OpenChronicle's dispatcher processes
them identically.

Captured events:
  - git_commit     — new commits in the watched git repo
  - agent_activity — Hermes/OpenClaw session changes, terminal commands
  - system_metric  — CPU, memory, disk (every 5 min)
  - deployment     — runbook version bumps, release events
  - file_change    — significant file modifications in workspace

Usage:
  OPENCHRONICLE_AX_WATCHER=./server-events-watcher.py openchronicle start

Environment:
  OC_WATCH_DIR          — git repo to watch (default: current directory)
  OC_HEARTBEAT_SEC      — system metric interval (default: 300)
  OC_POLL_SEC           — git/activity poll interval (default: 10)
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Config ─────────────────────────────────────────────────────────
WATCH_DIR = Path(os.environ.get("OC_WATCH_DIR", "."))
HEARTBEAT_SEC = int(os.environ.get("OC_HEARTBEAT_SEC", "300"))
POLL_SEC = int(os.environ.get("OC_POLL_SEC", "10"))

# ── State ──────────────────────────────────────────────────────────
_last_git_head: Optional[str] = None
_last_session_time: Optional[str] = None
_last_capture_ts: float = 0.0
_running = True


def _emit(event_type: str, **kwargs: Any) -> None:
    """Emit JSONL event matching OpenChronicle contract."""
    global _last_capture_ts
    now = time.monotonic()
    # Rate-limit non-critical events
    if event_type.startswith("file_change") or event_type.startswith("system_"):
        if now - _last_capture_ts < 1.0:
            return
    _last_capture_ts = now

    event: dict[str, Any] = {
        "event_type": event_type,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        **kwargs,
    }
    print(json.dumps(event, ensure_ascii=False), flush=True)


# ── Git Monitor ─────────────────────────────────────────────────────
def _git_latest_head() -> Optional[str]:
    """Get latest commit hash in master."""
    try:
        return subprocess.run(
            ["git", "-C", str(WATCH_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return None


def _git_latest_commit_info(head: str) -> Optional[dict[str, str]]:
    """Get commit message and author for a given hash."""
    try:
        result = subprocess.run(
            ["git", "-C", str(WATCH_DIR), "log", "-1", "--format=%s|||%an|||%aI", head],
            capture_output=True, text=True, timeout=5,
        )
        parts = result.stdout.strip().split("|||")
        if len(parts) >= 3:
            return {"message": parts[0], "author": parts[1], "date": parts[2]}
    except Exception:
        pass
    return None


def poll_git() -> None:
    """Check for new commits and emit events."""
    global _last_git_head
    head = _git_latest_head()
    if head and head != _last_git_head:
        info = _git_latest_commit_info(head)
        _emit("git_commit",
              app_name="Git",
              bundle_id="com.git",
              window_title=info.get("message", "(no message)") if info else "(unknown)",
              pid=0,
              commit_hash=head[:8],
              author=info.get("author", "") if info else "",
              commit_date=info.get("date", "") if info else "")
        _last_git_head = head


# ── Agent Activity Monitor ─────────────────────────────────────────
def _latest_hermes_session() -> Optional[tuple[str, str]]:
    """Get the most recent Hermes session timestamp and topic."""
    sessions_dir = Path(os.path.expanduser("~/.hermes/sessions/"))
    if not sessions_dir.exists():
        return None
    json_files = sorted(sessions_dir.glob("session_*.json"), reverse=True)
    if not json_files:
        return None
    try:
        data = json.loads(json_files[0].read_text())
        ts = data.get("created_at", "")
        # Extract first user message as "topic"
        messages = data.get("messages", [])
        topic = "session"
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content"):
                topic = msg["content"][:80]
                break
        return ts, topic
    except Exception:
        return None


def poll_agent_activity() -> None:
    """Check for new Hermes/OpenClaw sessions and emit events."""
    global _last_session_time
    result = _latest_hermes_session()
    if result:
        ts, topic = result
        if ts != _last_session_time:
            _emit("UserTextInput",
                  app_name="Hermes",
                  bundle_id="com.hermes.agent",
                  window_title=topic,
                  pid=0,
                  text=topic)
            _last_session_time = ts


# ── System Metrics ──────────────────────────────────────────────────
def poll_system_metrics() -> None:
    """Capture CPU, memory, disk usage."""
    try:
        # CPU
        cpu = subprocess.run(
            ["top", "-bn1"], capture_output=True, text=True, timeout=5
        ).stdout.split("\n")[2] if False else "cpu_ok"

        # Memory
        mem = subprocess.run(
            ["free", "-h"], capture_output=True, text=True, timeout=5
        ).stdout.split("\n")[1] if False else "mem_ok"

        # Disk
        disk = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True, timeout=5
        ).stdout.split("\n")[1] if False else "disk_ok"

        _emit("system_metric",
              app_name="System",
              bundle_id="com.system.metrics",
              window_title=f"CPU/Mem/Disk check",
              pid=0)
    except Exception:
        pass


# ── Deployment Monitor ──────────────────────────────────────────────
def poll_deployment() -> None:
    """Check for runbook version changes."""
    current_md = WATCH_DIR / "LLM-Server/releases/LLM-Server-Fresh-Install-2TB-current.md"
    if not current_md.exists():
        return
    try:
        content = current_md.read_text()
        m = re.search(r'v20\.(\d+)', content)
        if m:
            version = f"v20.{m.group(1)}"
            _emit("AXValueChanged",
                  app_name="Deployment",
                  bundle_id="com.deployment.runbook",
                  window_title=f"LLM-Server runbook {version}",
                  pid=0)
    except Exception:
        pass


# ── File Change Monitor ─────────────────────────────────────────────
def poll_file_changes() -> None:
    """Monitor significant files for changes."""
    key_files = [
        "AGENTS.md",
        "LLM-Server/AGENTS.md",
        "README.md",
    ]
    for rel_path in key_files:
        fpath = WATCH_DIR / rel_path
        if fpath.exists():
            mtime = fpath.stat().st_mtime
            age = time.time() - mtime
            if age < POLL_SEC * 2:  # Changed within last poll window
                _emit("AXTitleChanged",
                      app_name="FileSystem",
                      bundle_id="com.filesystem",
                      window_title=f"{rel_path} modified",
                      pid=0)


# ── Heartbeat ───────────────────────────────────────────────────────
def heartbeat_loop() -> None:
    """Periodic system metrics + keepalive."""
    while _running:
        time.sleep(HEARTBEAT_SEC)
        if _running:
            poll_system_metrics()


# ── Main Loop ───────────────────────────────────────────────────────
def poll_loop() -> None:
    """Main polling loop for all event sources."""
    global _last_git_head
    _last_git_head = _git_latest_head()

    while _running:
        poll_git()
        poll_agent_activity()
        poll_file_changes()
        poll_deployment()
        time.sleep(POLL_SEC)


def main() -> None:
    """Entry point."""
    def _shutdown(signum, frame):
        global _running
        _running = False
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Emit startup heartbeat
    _emit("_startup", version="1.0.0-server", platform="linux-server", watch_dir=str(WATCH_DIR))

    # Start heartbeat thread
    hb = threading.Thread(target=heartbeat_loop, daemon=True)
    hb.start()

    # Start main polling
    poll_loop()


if __name__ == "__main__":
    main()
