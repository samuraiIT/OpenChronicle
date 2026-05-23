# Hybrid Memory Model (Durable + Ambient)

OpenChronicle's **hybrid memory** model combines two layers into a single, searchable
memory surface for AI agents:

1. **Durable layer** (Hermes) — explicit, agent-written facts via `memory(action='add')`
2. **Ambient layer** (OpenChronicle) — auto-classified facts from screen/server capture

**Status:** ✅ Implemented (v20.90, May 2026)

## Priority Rule

| Layer | Role | Authority |
|---|---|---|
| **Durable** (Hermes) | Canon | Highest — if a fact exists in durable, it's truth |
| **Ambient** (OpenChronicle) | Evidence | Secondary — best available context when durable is silent |
| **Conflict** | — | Durable always wins |
| **Promotion** | ambient → durable | Agent promotes via `memory(action='add')` |

## File Structure

```
~/.hermes/memories/
├── MEMORY.md                     # Durable: agent notes
├── USER.md                       # Durable: user profile
└── ambient/ → ~/.openchronicle/memory/   # Symlink to ambient layer

~/.openchronicle/memory/
├── user-profile.md               # Auto-classified identity
├── user-preferences.md           # Auto-classified preferences
├── project-*.md                  # Per-project knowledge
├── tool-*.md                     # Tool-specific patterns
├── topic-*.md                    # Cross-cutting topics
├── event-YYYY-MM-DD.md           # Daily activity log
└── index.md                      # Auto-generated overview
```

## Setup

```bash
# Create symlink bridge
ln -sfn ~/.openchronicle/memory ~/.hermes/memories/ambient
```

Agents can now search ambient context:
- Hermes: `search_files(path="~/.hermes/memories/ambient/", pattern="dark theme")`
- Any MCP client: `hermetic_search(query="dark theme", layer="ambient")`

## Cross-Reference Semantics

Agent workflow for promoting ambient facts to durable:

```
1. Discover: search_files(path="~/.hermes/memories/ambient/", pattern="dark theme")
   → "User prefers dark theme in all editors"
2. Promote: memory(action='add', content='User prefers dark theme in all editors')
3. Result: fact now exists in both layers (durable canon + ambient evidence)
```

## Supersede Bridge

Both layers support fact retirement:

- **OpenChronicle:** `~~strikethrough~~` + `#superseded-by:{id}` metadata
- **Hermes:** `memory(action='replace', old_text=..., content=...)`

When OpenChronicle supersedes a fact, agents should check the durable layer
for corresponding entries and update them.

## Design Rationale

| Factor | Hermes-only | Hybrid |
|---|---|---|
| Memory quality | Depends on agent diligence | +Ambient context fills gaps |
| Token cost | ~0 (no LLM capture) | +LLM for timeline/reducer/classifier |
| Privacy | Server-local | Server-local (no cloud) |
| Search | `session_search` + `search_files` | +FTS5 BM25 + raw captures |
| Complexity | 1 system | 2 systems + bridge |
| Source of truth | Durable only | Durable canon + Ambient reference |

## Integration with AGENTS

The hybrid model is exposed to agents via `.agents/HERMES.md` and `.agents/OPENCLAW.md`:

```markdown
## Persistent Memory (Two-Layer)
- **Durable (Hermes):** MEMORY.md + USER.md — explicit writes via memory() tool
- **Ambient (OpenChronicle):** ~/.hermes/memories/ambient/ — auto-classified context
- **Rule:** Durable canon, Ambient evidence
```

## See Also

- [hermetic-mcp.md](hermetic-mcp.md) — Unified MCP server implementing the dual-layer search
- [server-mode.md](server-mode.md) — Server-side ambient capture
- [linux-port.md](linux-port.md) — Desktop Linux ambient capture
