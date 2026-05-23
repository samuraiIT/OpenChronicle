"""litellm wrapper with per-stage model resolution."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config, resolve_api_key
from ..logger import get

logger = get("openchronicle.writer")


def _parse_sse_response(text: str) -> dict[str, Any]:
    """Parse SSE (Server-Sent Events) stream into a dict matching OpenAI chat completion format.

    OmniRoute may return ``text/event-stream`` even when ``stream: false`` is set,
    especially with ``response_format: json_object`` and longer responses.

    Returns a dict with ``choices[0].message.content`` built from concatenated deltas,
    plus ``model`` and ``usage`` extracted from the final [DONE] event or last chunk.
    """
    import json as _json

    content_parts: list[str] = []
    model = ""
    usage = {}
    finish_reason = "stop"

    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            chunk = _json.loads(payload)
        except _json.JSONDecodeError:
            continue
        model = chunk.get("model", model)
        if "usage" in chunk:
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr
            delta = choice.get("delta", {})
            content = delta.get("content", "")
            if content:
                content_parts.append(content)

    return {
        "id": "chatcmpl-sse",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
            },
        }],
        "usage": usage,
    }


def _strip_json_fences(text: str) -> str:
    """Remove ```json / ``` fences that LLMs wrap around JSON output."""
    t = text.strip()
    if t.startswith("```"):
        # Find first newline after opening fence
        nl = t.find("\n")
        if nl > 0:
            t = t[nl + 1:]
        # Remove trailing ```
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3].rstrip()
    return t


@dataclass
class PingResult:
    stage: str
    model: str
    ok: bool
    latency_ms: int | None
    error: str | None
    mocked: bool = False


def _direct_chat_completion(
    *,
    model: str,
    base_url: str,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> Any:
    """Direct HTTP call to OpenAI-compatible endpoint with custom auth header.

    Bypasses litellm's Bearer-auth requirement. Returns a litellm-compatible
    ModelResponse object so callers (extract_text, extract_tool_calls) work unchanged.
    """
    import requests  # imported lazily

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,         # OmniRoute requires explicit stream:false
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    else:
        payload["max_tokens"] = 4096

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }

    url = base_url.rstrip("/") + "/chat/completions"
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()

    # OmniRoute may return SSE (text/event-stream) even with stream:false,
    # especially with response_format:json_object and longer responses.
    # Parse SSE chunks into a single JSON-compatible data dict.
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct or resp.text.lstrip().startswith("data:"):
        data = _parse_sse_response(resp.text)
    else:
        data = resp.json()

    # Strip ```json fences from content (Claude often wraps JSON in fences)
    for c in data.get("choices", []):
        msg = c.get("message", {})
        content = msg.get("content")
        if content:
            msg["content"] = _strip_json_fences(content)

    # Build litellm-compatible response wrapper
    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class _Choice:
        def __init__(self, msg, finish_reason="stop"):
            self.message = msg
            self.finish_reason = finish_reason

    class _Resp:
        def __init__(self, choices):
            self.choices = choices

    choices = []
    for c in data.get("choices", []):
        msg_data = c.get("message", {})
        content = msg_data.get("content")
        raw_tool_calls = msg_data.get("tool_calls", [])

        # Convert tool_calls dicts to litellm-style objects
        tool_calls = []
        for tc in raw_tool_calls:
            tc_id = tc.get("id", "")
            tc_type = tc.get("type", "function")
            fn = tc.get("function", {})
            fn_name = fn.get("name", "")
            fn_args = fn.get("arguments", "{}")

            class _ToolCall:
                def __init__(self, tc_id, tc_type, fn_name, fn_args):
                    self.id = tc_id
                    self.type = tc_type
                    self.function = _ToolFunction(fn_name, fn_args)

            class _ToolFunction:
                def __init__(self, name, arguments):
                    self.name = name
                    self.arguments = arguments

            tool_calls.append(_ToolCall(tc_id, tc_type, fn_name, fn_args))

        msg = _Msg(content=content, tool_calls=tool_calls)
        finish = c.get("finish_reason", "stop")
        choices.append(_Choice(msg, finish))

    return _Resp(choices)


def call_llm(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    json_mode: bool = False,
) -> Any:
    """Invoke litellm for the given stage. Returns the raw ModelResponse.

    Respects OPENCHRONICLE_LLM_MOCK=1 for tests: returns a minimal stub.
    """
    if os.environ.get("OPENCHRONICLE_LLM_MOCK") == "1":
        return _mock_response(stage, messages, tools, json_mode)

    import litellm  # imported lazily to keep CLI startup fast

    model_cfg = cfg.model_for(stage)
    kwargs: dict[str, Any] = {
        "model": model_cfg.model,
        "messages": messages,
    }
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url
    api_key = resolve_api_key(model_cfg)
    if api_key:
        kwargs["api_key"] = api_key
        # Direct HTTP when auth uses x-api-key (OmniRoute pattern)
        if model_cfg.base_url and api_key:
            return _direct_chat_completion(
                model=model_cfg.model.replace("openai/", ""),
                base_url=model_cfg.base_url,
                api_key=api_key,
                messages=messages,
                tools=tools,
                json_mode=json_mode,
                max_tokens=model_cfg.max_tokens,
            )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if model_cfg.max_tokens:
        kwargs["max_tokens"] = model_cfg.max_tokens

    logger.debug("llm call stage=%s model=%s", stage, model_cfg.model)
    return litellm.completion(**kwargs)


def _mock_response(stage: str, messages, tools, json_mode):
    """Minimal stub for offline tests. Customize via OPENCHRONICLE_LLM_MOCK_JSON."""
    override = os.environ.get("OPENCHRONICLE_LLM_MOCK_JSON")
    content = override if override else '{"worth_writing": false, "brief_reason": "mock"}'

    class _Msg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, msg):
            self.message = msg
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, choices):
            self.choices = choices

    return _Resp([_Choice(_Msg(content))])


def extract_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def ping_stage(cfg: Config, stage: str, *, timeout: float = 5.0) -> PingResult:
    """Send a tiny round-trip request to the stage's configured model.

    Returns a PingResult with success, latency, and a short error label on
    failure. Honors OPENCHRONICLE_LLM_MOCK=1 by returning a mocked-ok result
    without touching the network. Never raises — `status` and similar
    informational callers must remain non-fatal.
    """
    model_cfg = cfg.model_for(stage)
    if os.environ.get("OPENCHRONICLE_LLM_MOCK") == "1":
        return PingResult(
            stage=stage, model=model_cfg.model, ok=True,
            latency_ms=0, error=None, mocked=True,
        )

    try:
        import litellm  # lazy import — keeps CLI startup fast
    except ImportError as exc:
        return PingResult(
            stage=stage, model=model_cfg.model, ok=False,
            latency_ms=None, error=f"ImportError: {exc}",
        )

    kwargs: dict[str, Any] = {
        "model": model_cfg.model,
        "messages": [{"role": "user", "content": "Reply with 'ok'."}],
        "max_tokens": 4,
        "timeout": timeout,
    }
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url
    api_key = resolve_api_key(model_cfg)
    if api_key:
        kwargs["api_key"] = api_key

    # Direct HTTP for OmniRoute (which uses x-api-key, not Bearer)
    if model_cfg.base_url and api_key:
        start = time.monotonic()
        try:
            _direct_chat_completion(
                model=model_cfg.model.replace("openai/", ""),
                base_url=model_cfg.base_url,
                api_key=api_key,
                messages=[{"role": "user", "content": "Reply with 'ok'."}],
                max_tokens=4,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            return PingResult(
                stage=stage, model=model_cfg.model, ok=True,
                latency_ms=latency_ms, error=None,
            )
        except Exception as exc:
            label = type(exc).__name__
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            if msg:
                label = f"{label}: {msg[:60]}"
            return PingResult(
                stage=stage, model=model_cfg.model, ok=False,
                latency_ms=None, error=label[:80],
            )

    start = time.monotonic()
    try:
        litellm.completion(**kwargs)
    except Exception as exc:  # noqa: BLE001
        label = type(exc).__name__
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        if msg:
            label = f"{label}: {msg[:60]}"
        return PingResult(
            stage=stage, model=model_cfg.model, ok=False,
            latency_ms=None, error=label[:80],
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    return PingResult(
        stage=stage, model=model_cfg.model, ok=True,
        latency_ms=latency_ms, error=None,
    )


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    try:
        calls = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError):
        return []
    out: list[dict[str, Any]] = []
    for c in calls:
        fn = getattr(c, "function", None) or c.get("function", {})
        args_raw = getattr(fn, "arguments", None) if hasattr(fn, "arguments") else fn.get("arguments")
        name = getattr(fn, "name", None) if hasattr(fn, "name") else fn.get("name")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        out.append(
            {
                "id": getattr(c, "id", None) or c.get("id"),
                "name": name,
                "arguments": args,
            }
        )
    return out
