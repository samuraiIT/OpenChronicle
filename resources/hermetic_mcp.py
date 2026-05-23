#!/usr/bin/env python3
"""hermetic_mcp.py — Unified Memory MCP Server for Hermes + OpenChronicle.

Two-layer search server:
  1. Durable layer (Hermes) — MEMORY.md, USER.md, state.db
  2. Ambient layer (OpenChronicle) — user-*.md, project-*.md, etc. + FTS5

MCP clients (Claude Code, Codex, Cursor, Hermes) connect via:
  stdio: python3 hermetic_mcp.py

Tools:
  - hermetic_search(query)   — unified search across durable + ambient
  - hermetic_read(path)      — read memory file (durable or ambient)
  - hermetic_list()          — list all memory files
  - hermetic_context()       — recent activity from ambient layer
  - suggest_runbook_update() — read-only bridge proposal for LLM-Server (V3)

Port: MCP stdio protocol. Configure in ~/.hermes/mcp.json.
"""

import json
import os
import re
import sys
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

# ── Paths ───────────────────────────────────────────────────────────────
HERMES_MEMORY_DIR = Path(os.environ.get(
    "HERMES_MEMORY_DIR",
    os.path.expanduser("~/.hermes/memories")
))
OPENCHRONICLE_DIR = Path(os.environ.get(
    "OPENCHRONICLE_DIR",
    os.path.expanduser("~/.openchronicle/memory")
))
OPENCHRONICLE_DB = Path(os.environ.get(
    "OPENCHRONICLE_DB",
    os.path.expanduser("~/.openchronicle/index.db")
))
WORKSPACE_ROOT = Path(os.environ.get(
    "WORKSPACE_ROOT",
    os.path.expanduser("~/workspace")
))

FTS5_SPECIALS = set('":*()^+-')
_VERSION_DOT_RE = re.compile(r'\bv(\d+)\.(\d+)\b')


def _safe_fts_query(query: str) -> str:
    """Convert free text into a safe FTS5 MATCH expression.

    Normalizes version strings (v20.94 -> v20_94) so FTS5 can search
    dotted version numbers without syntax errors near ".".
    """
    # Normalize version patterns: v20.94 -> v20_94 (FTS5-safe single token)
    query = _VERSION_DOT_RE.sub(r'v\1_\2', query)
    tokens = []
    for raw in query.split():
        cleaned = "".join(ch for ch in raw if ch not in FTS5_SPECIALS)
        if cleaned:
            tokens.append(f'"{cleaned}"')
    return " ".join(tokens) if tokens else '""'


def _excerpt(text: str, query: str, limit: int = 700) -> str:
    """Return a readable excerpt around the first query token."""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact

    terms = [t for t in re.split(r"\s+", query) if t]
    lower = compact.lower()
    for term in terms:
        idx = lower.find(term.lower())
        if idx >= 0:
            start = max(0, idx - limit // 3)
            end = min(len(compact), start + limit)
            snippet = compact[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(compact):
                snippet = snippet + "..."
            return snippet
    return compact[:limit] + "..."


def _search_files(path: Path, pattern: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search inside Markdown files using ripgrep."""
    try:
        result = subprocess.run(
            ["rg", "--json", "--max-count", str(limit), "-i", pattern, str(path)],
            capture_output=True, text=True, timeout=10
        )
        results = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    m = data.get("data", {})
                    results.append({
                        "path": m.get("path", {}).get("text", ""),
                        "line_number": m.get("line_number", 0),
                        "text": m.get("lines", {}).get("text", "").strip(),
                    })
            except json.JSONDecodeError:
                pass
        return results[:limit]
    except Exception as exc:
        return [{"error": str(exc)}]


def _fts5_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search OpenChronicle ambient layers: entries FTS + captures FTS."""
    if not OPENCHRONICLE_DB.exists():
        return [{"error": f"OpenChronicle DB not found: {OPENCHRONICLE_DB}"}]

    safe_query = _safe_fts_query(query)
    if safe_query == '""':
        return []

    try:
        conn = sqlite3.connect(f"file:{OPENCHRONICLE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        results: list[dict[str, Any]] = []

        cursor.execute(
            """
            SELECT id, path, timestamp, tags, content, bm25(entries) AS rank
            FROM entries
            WHERE entries MATCH ?
              AND superseded = 0
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit),
        )
        for row in cursor.fetchall():
            results.append({
                "kind": "entry",
                "id": row["id"],
                "path": row["path"],
                "body": _excerpt(row["content"], query),
                "tags": row["tags"] or "",
                "timestamp": row["timestamp"],
                "rank": row["rank"],
            })

        cursor.execute(
            """
            SELECT c.id, c.timestamp, c.app_name, c.window_title, c.url,
                   snippet(captures_fts, -1, '[', ']', '…', 16) AS snippet,
                   bm25(captures_fts) AS rank
            FROM captures c
            JOIN captures_fts ON captures_fts.rowid = c.rowid
            WHERE captures_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit),
        )
        for row in cursor.fetchall():
            results.append({
                "kind": "capture",
                "id": row["id"],
                "path": f"capture-buffer/{row['id']}.json",
                "body": row["snippet"] or "",
                "tags": row["app_name"] or "",
                "timestamp": row["timestamp"],
                "rank": row["rank"],
                "window_title": row["window_title"] or "",
                "url": row["url"] or "",
            })
        conn.close()
        return results[: limit * 2]
    except Exception as exc:
        return [{"error": f"FTS5 search failed: {exc}"}]


def _read_file(path_str: str, tail_n: int = 0) -> dict[str, Any]:
    """Read memory file content."""
    path = Path(path_str)
    if not path.is_absolute():
        for base in [HERMES_MEMORY_DIR, OPENCHRONICLE_DIR, WORKSPACE_ROOT]:
            candidate = base / path_str
            if candidate.exists():
                path = candidate
                break

    if not path.exists():
        return {"error": f"File not found: {path_str}"}

    try:
        content = path.read_text()
        lines = content.split("\n")
        if tail_n > 0:
            lines = lines[-tail_n:]

        return {
            "path": str(path),
            "lines": len(lines),
            "content": "\n".join(lines),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _list_memories() -> list[dict[str, Any]]:
    """List all memory files: durable + ambient."""
    files = []

    # Durable layer — Hermes
    for f in HERMES_MEMORY_DIR.glob("*.md"):
        if f.name.startswith("."):
            continue
        files.append({
            "path": str(f),
            "layer": "durable",
            "name": f.name,
            "size": f.stat().st_size,
        })

    # Ambient layer — OpenChronicle (via symlink)
    ambient_symlink = HERMES_MEMORY_DIR / "ambient"
    if ambient_symlink.is_symlink() or ambient_symlink.is_dir():
        for f in ambient_symlink.rglob("*.md"):
            rel_path = f.relative_to(ambient_symlink)
            files.append({
                "path": f"ambient/{rel_path}",
                "layer": "ambient",
                "name": f.name,
                "size": f.stat().st_size,
            })

    return sorted(files, key=lambda x: x["name"])


def _run_runbook_suggestion() -> dict[str, Any]:
    """Run the workspace bridge proposal script and return structured JSON.

    This is the V3 hybrid bridge: OpenChronicle reads LLM-Server runbook,
    detects drift (stale version references, changed file structure),
    and proposes updates — but never writes directly.
    """
    script_path = WORKSPACE_ROOT / "scripts" / "suggest_runbook_update.py"
    if not script_path.exists():
        return {"error": f"Suggestion script not found: {script_path}"}

    try:
        result = subprocess.run(
            ["python3", str(script_path), "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        return {"error": f"Suggestion tool failed to start: {exc}"}

    if result.returncode != 0:
        return {
            "error": "Suggestion tool returned non-zero exit status",
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "error": f"Suggestion tool returned invalid JSON: {exc}",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }


# ── MCP Server ──────────────────────────────────────────────────────────


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "hermetic_search",
                        "description": "Unified search across durable (Hermes) and ambient (OpenChronicle) memory layers. Use for ANY question about user preferences, past work, projects, tools, or people.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search keywords (ripgrep syntax)"},
                                "limit": {"type": "integer", "description": "Max results", "default": 10},
                                "layer": {"type": "string", "enum": ["all", "durable", "ambient"], "default": "all"},
                                "use_fts5": {"type": "boolean", "description": "Use FTS5 for ambient layer when available", "default": True},
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "hermetic_read",
                        "description": "Read the contents of one memory file (durable or ambient layer).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "File path or name (e.g. 'MEMORY.md' or 'ambient/user-preferences.md')"},
                                "tail_n": {"type": "integer", "description": "Read last N lines only", "default": 0},
                            },
                            "required": ["path"],
                        },
                    },
                    {
                        "name": "hermetic_list",
                        "description": "List all memory files across both layers (durable + ambient).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                    {
                        "name": "hermetic_context",
                        "description": "Get recent ambient activity context — what the user has been working on.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "description": "Max entries", "default": 10},
                            },
                        },
                    },
                    {
                        "name": "suggest_runbook_update",
                        "description": "Read-only proposal layer for the LLM-Server ↔ OpenChronicle V3 bridge. Returns drift analysis and suggested commands without writing files.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                ]
            },
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "hermetic_search":
            query = arguments.get("query", "")
            limit = arguments.get("limit", 10)
            layer = arguments.get("layer", "all")
            use_fts5 = arguments.get("use_fts5", True)

            results = []
            if layer in ("all", "durable"):
                durable = _search_files(HERMES_MEMORY_DIR, query, limit)
                for r in durable:
                    r["layer"] = "durable"
                    results.append(r)

            if layer in ("all", "ambient"):
                if use_fts5 and OPENCHRONICLE_DB.exists():
                    ambient = _fts5_search(query, limit)
                else:
                    ambient = _search_files(OPENCHRONICLE_DIR, query, limit)
                for r in ambient:
                    r["layer"] = "ambient"
                    results.append(r)

            text = json.dumps(results, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }

        elif tool_name == "hermetic_read":
            result = _read_file(arguments.get("path", ""), arguments.get("tail_n", 0))
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }

        elif tool_name == "hermetic_list":
            files = _list_memories()
            text = json.dumps({"files": files, "count": len(files)}, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }

        elif tool_name == "hermetic_context":
            limit = arguments.get("limit", 10)
            event_files = sorted(OPENCHRONICLE_DIR.glob("event-*.md"), reverse=True)
            context_lines = []
            for f in event_files[:3]:
                content = f.read_text()
                lines = content.split("\n")[-20:]
                context_lines.extend(lines)

            text = "\n".join(context_lines[-limit * 5:])
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }

        elif tool_name == "suggest_runbook_update":
            result = _run_runbook_suggestion()
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main() -> None:
    """MCP stdio server main loop."""
    for line in sys.stdin:
        try:
            request = json.loads(line.strip())
            response = handle_request(request)
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except BrokenPipeError:
            break


if __name__ == "__main__":
    main()
