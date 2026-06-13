"""Claude JSON export parser (claude.ai data export format).

Handles:
- A single conversation object with a "chat_messages" array, where each
  message has "sender" ("human"/"assistant"), "text" and/or a "content"
  array of blocks, and "created_at".
- A bulk export: a top-level JSON array of such conversation objects.
- API-style transcripts with a "messages" array of {role, content} as a
  fallback (content may be a string or a list of content blocks).

Tool-use blocks, attachments, and other non-text metadata are stripped.
"""

import json
import logging

log = logging.getLogger("atomizer.parsers.claude_json")


def matches(data) -> bool:
    """Structural check used by format auto-detection."""
    candidates = data if isinstance(data, list) else [data]
    for item in candidates[:5]:
        if isinstance(item, dict) and ("chat_messages" in item or "messages" in item):
            return True
    return False


def _text_from_content(content) -> str:
    """Extract plain text from a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # Keep text blocks; skip tool_use/tool_result/thinking noise.
                if block.get("type") in (None, "text") and block.get("text"):
                    parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _normalize_role(sender: str) -> str | None:
    sender = (sender or "").lower()
    if sender in ("human", "user"):
        return "human"
    if sender in ("assistant", "ai"):
        return "assistant"
    return None


def _parse_conversation(conv: dict):
    """Parse one conversation object into (session_name, turns)."""
    session_name = conv.get("name") or conv.get("title") or None
    messages = conv.get("chat_messages") or conv.get("messages") or []
    turns = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = _normalize_role(msg.get("sender") or msg.get("role") or "")
        if role is None:
            continue  # skip system/tool messages
        text = msg.get("text") or _text_from_content(msg.get("content"))
        text = (text or "").strip()
        if not text:
            continue
        timestamp = msg.get("created_at") or msg.get("updated_at") or None
        turns.append({"role": role, "content": text, "timestamp": timestamp})
    return session_name, turns


def parse(text: str) -> list:
    """Returns [(session_name, turns), ...]."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Input is not valid JSON: {exc}") from exc

    conversations = data if isinstance(data, list) else [data]
    results = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        name, turns = _parse_conversation(conv)
        if turns:
            results.append((name, turns))
        else:
            log.debug("Skipping conversation with no extractable turns: %r", name)

    log.info(
        "Claude JSON parser extracted %d conversation(s), %d turn(s) total",
        len(results), sum(len(t) for _, t in results),
    )
    return results
