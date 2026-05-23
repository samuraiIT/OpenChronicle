# Hermetic MCP Server

`hermetic_mcp.py` is a unified MCP (Model Context Protocol) server that provides
**dual-layer memory search** across both Hermes durable memory and OpenChronicle
ambient memory through a single interface.

**Status:** ✅ Complete (May 2026)

**File:** `resources/hermetic_mcp.py`

## Architecture

```
MCP Client (Claude Code, Codex, Cursor, Hermes)
        │
        │ stdio (JSON-RPC)
        ▼
┌─────────────────────────────────────┐
│        hermetic_mcp.py              │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  Durable Layer (Hermes)       │  │
│  │  MEMORY.md, USER.md           │  │
│  │  ripgrep search               │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │  Ambient Layer (OpenChronicle)│  │
│  │  FTS5 + captures              │  │
│  │  SQLite index.db              │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

## Tools

### `hermetic_search`

Search across both memory layers:

```json
{
  "name": "hermetic_search",
  "arguments": {
    "query": "v20.94",
    "limit": 10,
    "layer": "all",
    "use_fts5": true
  }
}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | *required* | Search keywords (ripgrep syntax for durable, FTS5 for ambient) |
| `limit` | integer | 10 | Max results per layer |
| `layer` | string | `"all"` | `"durable"`, `"ambient"`, or `"all"` |
| `use_fts5` | boolean | `true` | Use FTS5 BM25 for ambient layer (falls back to ripgrep) |

### `hermetic_read`

Read a memory file from either layer:

```json
{
  "name": "hermetic_read",
  "arguments": {
    "path": "MEMORY.md",
    "tail_n": 50
  }
}
```

`path` can be:
- Relative (`MEMORY.md`) — searched in durable → ambient → workspace order
- Absolute (`~/.hermes/memories/MEMORY.md`)

### `hermetic_list`

List all memory files across both layers:

```json
{"name": "hermetic_list", "arguments": {}}
```

Returns: `{"files": [...], "count": N}` with `layer`, `name`, `size` per file.

### `hermetic_context`

Get recent ambient activity context:

```json
{
  "name": "hermetic_context",
  "arguments": {"limit": 10}
}
```

Reads the last 3 `event-*.md` files and returns the most recent entries.

### `suggest_runbook_update`

Read-only bridge proposal for LLM-Server runbook integration (V3 bridge):

```json
{"name": "suggest_runbook_update", "arguments": {}}
```

Returns drift analysis and suggested update commands — **never writes files**.

## Configuration

All paths are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `HERMES_MEMORY_DIR` | `~/.hermes/memories` | Durable layer directory |
| `OPENCHRONICLE_DIR` | `~/.openchronicle/memory` | Ambient layer directory |
| `OPENCHRONICLE_DB` | `~/.openchronicle/index.db` | FTS5 SQLite database |
| `WORKSPACE_ROOT` | `/workspace` | Workspace root for script resolution |

## FTS5 Dot-Syntax Normalization

The server automatically normalizes version patterns in queries to prevent FTS5
syntax errors:

```
Input:  "v20.94"      →  FTS5 query: "v20_94"
Input:  "v1.2.3"      →  FTS5 query: "v1_2_3"
```

Without normalization, `v20.94` in an FTS5 MATCH expression causes:
`fts5: syntax error near "."` — the dot triggers float-literal parsing.

## Integration

Add to MCP client configuration:

```json
// ~/.hermes/mcp.json
{
  "mcpServers": {
    "hermetic": {
      "command": "python3",
      "args": ["resources/hermetic_mcp.py"],
      "env": {
        "HERMES_MEMORY_DIR": "/home/user/.hermes/memories",
        "OPENCHRONICLE_DIR": "/home/user/.openchronicle/memory",
        "OPENCHRONICLE_DB": "/home/user/.openchronicle/index.db"
      }
    }
  }
}
```

## Verification

```bash
# Test search
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hermetic_search","arguments":{"query":"v20.94"}}}' \
  | python3 resources/hermetic_mcp.py

# Test list
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hermetic_list","arguments":{}}}' \
  | python3 resources/hermetic_mcp.py
```

## See Also

- [hybrid-memory.md](hybrid-memory.md) — Dual-layer memory architecture
- [mcp.md](mcp.md) — OpenChronicle's built-in MCP server (capture tools)
- [server-mode.md](server-mode.md) — Headless server ambient capture
