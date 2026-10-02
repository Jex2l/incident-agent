"""Shared helpers for MCP tools: all tools must return trimmed, structured
JSON so a small local model doesn't drown in context. Every tool caps its
own output; this module provides the generic truncation helper.
"""
import json
import os

MAX_OUTPUT_CHARS = int(os.environ.get("TOOL_MAX_OUTPUT_CHARS", "4000"))


def cap_list(items: list, max_items: int) -> tuple[list, bool]:
    """Truncate a list to max_items, returning (items, was_truncated)."""
    if len(items) <= max_items:
        return items, False
    return items[:max_items], True


def cap_json(obj) -> dict:
    """Serialize obj to JSON; if it exceeds MAX_OUTPUT_CHARS, re-serialize
    with a note instead of silently handing a huge blob to the model.
    """
    text = json.dumps(obj)
    if len(text) <= MAX_OUTPUT_CHARS:
        return obj
    return {
        "truncated": True,
        "reason": f"full output was {len(text)} chars, exceeding the {MAX_OUTPUT_CHARS} char cap",
        "preview": text[:MAX_OUTPUT_CHARS],
    }
