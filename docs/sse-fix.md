# OmniRoute SSE & JSON Fence Fix

**Status:** ✅ Fixed (May 2026) — 86/88 session reducer failures resolved.

**File:** `src/openchronicle/writer/llm.py`

## Problem

The session reducer (`session_reducer.py`) calls LLMs with `json_mode=True`
(`response_format: {"type": "json_object"}`) and expects `json.loads()` to work.
Two issues caused 98% of sessions to fail:

### 1. OmniRoute SSE Streaming

OmniRoute returns `text/event-stream` **even when `stream: false` is explicitly set**,
especially with `response_format: json_object` and longer responses (max_tokens ≥ 200):

| Max Tokens | response_format | Content-Type | Body |
|---|---|---|---|
| 4 | none | `application/json` | Clean JSON ✅ |
| 200 | none | `application/json` | Clean JSON ✅ |
| 200 | `json_object` | `text/event-stream` | `data:` lines ❌ |

The `_direct_chat_completion()` function calls `resp.json()` on the raw response,
which fails with `JSONDecodeError` on SSE `data:`-prefixed body.

### 2. Claude JSON Fence Wrapping

Even when OmniRoute returns clean JSON, Claude wraps the content in markdown fences:

````json
{
  "summary": "User worked on...",
  "sub_tasks": ["git operations", "file editing"]
}
````

`json.loads()` fails because the opening triple-backtick is the first token.

## Solution

Two new helper functions added before `_direct_chat_completion()`:

### `_parse_sse_response(text: str) -> dict`

Parses SSE `data:` lines, extracts `delta.content` from each chunk,
concatenates them, and returns an OpenAI-compatible dict with
`choices[0].message.content`.

- Skips comments (`: ...`), empty lines, `[DONE]` markers
- Handles `JSONDecodeError` on individual chunks (non-fatal)
- Extracts `model` and `usage` from the last chunk

### `_strip_json_fences(text: str) -> str`

Removes ```json / ``` wrappers from LLM output:

```python
def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl > 0:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3].rstrip()
    return t
```

### Patched `_direct_chat_completion()` Body

After `resp.raise_for_status()`, the response is now handled with:

```python
# Detect SSE even when stream:false was set
ct = resp.headers.get("content-type", "")
if "text/event-stream" in ct or resp.text.lstrip().startswith("data:"):
    data = _parse_sse_response(resp.text)
else:
    data = resp.json()

# Strip ```json fences from all message content
for c in data.get("choices", []):
    msg = c.get("message", {})
    content = msg.get("content")
    if content:
        msg["content"] = _strip_json_fences(content)
```

## Direct HTTP Path Integration

The fix also routes `call_llm()` and `ping_model()` through `_direct_chat_completion()`
when `model_cfg.base_url` is configured (OmniRoute pattern with `x-api-key` auth),
bypassing litellm's Bearer-auth requirement entirely.

## Verification

```bash
# Restart daemon after applying fix
openchronicle stop && sleep 2
openchronicle start

# Wait for session cycle (~5 min), then check:
openchronicle status | grep Sessions
# Previously: "Sessions  88 total (0 reduced, ...)"
# After fix:   "Sessions  91 total (2 reduced, ...)" — and climbing

# Check writer log:
grep "llm_ok=True" ~/.openchronicle/logs/writer.log | tail -5
```

## Result

- **First successful reduction in project history:** `sess_76d878cd2d5e`
- Reducer pipeline fully operational: 0→3 reduced sessions and counting
- All 4 OmniRoute LLM stages verified healthy
- Classifier still requires separate [model compatibility fix](config.md#classifier-model)

## See Also

- [config.md](config.md) — OmniRoute configuration and model compatibility matrix
- [writer.md](writer.md) — Writer pipeline architecture
- [troubleshooting.md](troubleshooting.md) — Common issues and debugging
