"""Shared helpers for MCP tools: all tools must return trimmed, structured
JSON so a small local model doesn't drown in context. Every tool caps its
own output; this module provides the generic truncation helper.
"""
import json
import os

# Global ceiling on how many characters of JSON a single tool call is allowed
# to hand back to the model. Configurable via env var so it can be tuned per
# deployment (a bigger local model with more context headroom could raise
# this), but the default of 4000 chars is sized for small local models where
# a single oversized tool result can crowd out the rest of the investigation
# history.
MAX_OUTPUT_CHARS = int(os.environ.get("TOOL_MAX_OUTPUT_CHARS", "4000"))


def cap_list(items: list, max_items: int) -> tuple[list, bool]:
    """Truncate a list to max_items, returning (items, was_truncated).

    This is the item-count cap (used for things like "top 20 pods" or "top
    10 log patterns") as opposed to cap_json's character-count cap below -
    callers typically apply this first to bound the number of entries, then
    may still run the result through cap_json as a final safety net against
    any one entry being unexpectedly large.
    """
    if len(items) <= max_items:
        return items, False
    return items[:max_items], True


def cap_json(obj) -> dict:
    """Serialize obj to JSON; if it exceeds MAX_OUTPUT_CHARS, re-serialize
    with a note instead of silently handing a huge blob to the model.

    This is a last-resort safety net: cap_list already bounds the number of
    items most tools return, but a handful of large fields (e.g. a pod log
    line, an error message) could still blow past the character budget. Only
    emit the oversized object (and crucially, how it failed) if it's small
    enough to be fine; otherwise return a small "truncated" envelope with a
    preview so the agent at least sees partial content and knows why the
    rest is missing, rather than erroring out or stalling the model on a
    huge payload.
    """
    text = json.dumps(obj)
    if len(text) <= MAX_OUTPUT_CHARS:
        return obj
    return {
        "truncated": True,
        "reason": f"full output was {len(text)} chars, exceeding the {MAX_OUTPUT_CHARS} char cap",
        "preview": text[:MAX_OUTPUT_CHARS],
    }
