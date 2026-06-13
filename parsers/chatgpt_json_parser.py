"""ChatGPT export parser (OpenAI conversations.json format).

The export is a JSON array of conversations (or a single conversation
object). Each conversation has a "mapping" of node_id -> node, where a node
holds {message, parent, children}. The active branch is reconstructed by
walking up from "current_node" to the root (falling back to a root-down walk
choosing the last child when "current_node" is absent).

System messages, tool messages, and empty visual placeholders are stripped.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("atomizer.parsers.chatgpt_json")


def matches(data) -> bool:
    """Structural check used by format auto-detection."""
    candidates = data if isinstance(data, list) else [data]
    for item in candidates[:5]:
        if isinstance(item, dict) and isinstance(item.get("mapping"), dict):
            return True
    return False


def _iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (ValueError, OSError, OverflowError):
        return None


def _ordered_nodes(conv: dict) -> list:
    """Return message nodes of the active branch in chronological order."""
    mapping = conv.get("mapping") or {}

    current = conv.get("current_node")
    if current and current in mapping:
        # Walk up from the leaf of the active branch.
        chain = []
        node_id = current
        seen = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            chain.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
        return list(reversed(chain))

    # No current_node: find the root and walk down, taking the last child at
    # each branch point (the most recent edit/regeneration).
    root_id = next(
        (nid for nid, node in mapping.items() if not node.get("parent")),
        None,
    )
    chain = []
    node_id = root_id
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        chain.append(node)
        children = node.get("children") or []
        node_id = children[-1] if children else None
    return chain


def _text_from_message(msg: dict) -> str:
    content = msg.get("content") or {}
    ctype = content.get("content_type")
    if ctype == "text":
        parts = content.get("parts") or []
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    if ctype == "multimodal_text":
        parts = content.get("parts") or []
        texts = [p for p in parts if isinstance(p, str)]
        return "\n".join(texts).strip()
    if ctype == "code":
        text = content.get("text") or ""
        lang = content.get("language") or ""
        return f"```{lang}\n{text}\n```".strip() if text else ""
    # user_editable_context, tether_quote, execution_output, etc. → noise.
    return ""


def _parse_conversation(conv: dict):
    session_name = conv.get("title") or None
    turns = []
    for node in _ordered_nodes(conv):
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        role = ((msg.get("author") or {}).get("role") or "").lower()
        if role == "user":
            norm_role = "human"
        elif role == "assistant":
            norm_role = "assistant"
        else:
            continue  # system / tool messages are noise
        meta = msg.get("metadata") or {}
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        text = _text_from_message(msg)
        if not text:
            continue
        turns.append({
            "role": norm_role,
            "content": text,
            "timestamp": _iso(msg.get("create_time")),
        })
    return session_name, turns


def parse(text: str) -> list:
    """Returns [(session_name, turns), ...] — one entry per conversation."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Input is not valid JSON: {exc}") from exc

    conversations = data if isinstance(data, list) else [data]
    results = []
    for conv in conversations:
        if not isinstance(conv, dict) or "mapping" not in conv:
            continue
        name, turns = _parse_conversation(conv)
        if turns:
            results.append((name, turns))
        else:
            log.debug("Skipping conversation with no extractable turns: %r", name)

    log.info(
        "ChatGPT JSON parser extracted %d conversation(s), %d turn(s) total",
        len(results), sum(len(t) for _, t in results),
    )
    return results
