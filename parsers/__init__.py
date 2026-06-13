"""Parser package: format auto-detection and dispatch.

All parsers normalize input into a list of Conversation objects, where each
conversation holds turns of the shape:

    {"role": "human" | "assistant", "content": str, "timestamp": str | None}

A single input file may yield multiple conversations (e.g. a ChatGPT bulk
conversations.json export); each conversation is atomized independently.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from parsers import (
    chatgpt_json_parser,
    claude_json_parser,
    copilot_parser,
    docx_parser,
    markdown_parser,
    pdf_parser,
    plaintext_parser,
)

log = logging.getLogger("atomizer.parsers")

FORMATS = ("claude-json", "chatgpt-json", "markdown", "copilot", "plaintext",
           "pdf", "docx")

# Formats parsed from the file path (binary), not from decoded text.
_BINARY_FORMATS = {"pdf", "docx"}


@dataclass
class Conversation:
    turns: list = field(default_factory=list)
    session_name: str | None = None


_PARSER_MODULES = {
    "claude-json": claude_json_parser,
    "chatgpt-json": chatgpt_json_parser,
    "markdown": markdown_parser,
    "copilot": copilot_parser,
    "plaintext": plaintext_parser,
    "pdf": pdf_parser,
    "docx": docx_parser,
}


def detect_format(path: Path, text: str) -> str:
    """Auto-detect the input format from file extension and content."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"

    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning(
                "%s has a .json extension but is not valid JSON; treating "
                "as plaintext.", path.name,
            )
            return "plaintext"
        if chatgpt_json_parser.matches(data):
            return "chatgpt-json"
        if claude_json_parser.matches(data):
            return "claude-json"
        log.warning(
            "%s is JSON but matches neither the Claude nor the ChatGPT "
            "export structure; trying claude-json as a best effort. Use "
            "--format to override.", path.name,
        )
        return "claude-json"

    # .md / .txt / anything else: content sniffing.
    if copilot_parser.matches(text):
        return "copilot"
    if suffix == ".md":
        return "markdown"
    return "plaintext"


def parse_file(path: Path, fmt: str | None = None) -> tuple[str, list[Conversation]]:
    """Parse an input file. Returns (format_used, conversations).

    fmt of None or "auto" triggers auto-detection.
    """
    text = None
    if fmt in (None, "", "auto"):
        if path.suffix.lower() in (".pdf", ".docx"):
            fmt = detect_format(path, "")
        else:
            # utf-8-sig transparently strips the BOM Windows tools often add.
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            fmt = detect_format(path, text)
        log.info("Auto-detected format for %s: %s", path.name, fmt)
    elif fmt not in _PARSER_MODULES:
        raise ValueError(
            f"Unknown format '{fmt}'. Valid formats: {', '.join(FORMATS)}"
        )
    else:
        log.info("Using explicit format for %s: %s", path.name, fmt)

    if fmt in _BINARY_FORMATS:
        raw_conversations = _PARSER_MODULES[fmt].parse_path(path)
    else:
        if text is None:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        raw_conversations = _PARSER_MODULES[fmt].parse(text)
    conversations = [
        Conversation(turns=turns, session_name=name)
        for name, turns in raw_conversations
        if turns
    ]

    total_turns = sum(len(c.turns) for c in conversations)
    log.info(
        "Parser '%s' extracted %d conversation(s), %d turn(s) total from %s",
        fmt, len(conversations), total_turns, path.name,
    )
    if not conversations:
        log.warning("No conversational content extracted from %s", path.name)
    return fmt, conversations
