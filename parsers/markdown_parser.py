"""Raw Markdown conversation parser.

Detects speaker turns by common patterns: "Human:", "Assistant:", "User:",
"AI:", "Ed:", bold variants ("**Human:**"), and heading-delimited turns
("## Human"). Strips common HTML tags and chat-UI artifacts.
"""

import logging
import re

log = logging.getLogger("atomizer.parsers.markdown")

# Speaker labels mapped to normalized roles.
HUMAN_LABELS = {"human", "user", "ed", "you", "me", "q", "question"}
ASSISTANT_LABELS = {
    "assistant", "ai", "claude", "chatgpt", "gpt", "bot", "model",
    "a", "answer", "copilot", "bing", "gemini",
}
_ALL_LABELS = HUMAN_LABELS | ASSISTANT_LABELS

# "Human:", "**Human:**", "Human :", "__User__:" etc. at line start.
_INLINE_SPEAKER_RE = re.compile(
    r"^\s*(?:[*_]{1,2})?(?P<label>[A-Za-z][A-Za-z .]{0,30}?)(?:[*_]{1,2})?"
    r"\s*[:：]\s*(?P<rest>.*)$"
)
# "## Human", "### Assistant" heading-delimited turns.
_HEADING_SPEAKER_RE = re.compile(
    r"^\s*#{1,6}\s+(?P<label>[A-Za-z][A-Za-z .]{0,30}?)\s*:?\s*$"
)

# Common HTML tags only — a blanket <[^>]+> regex would destroy code like
# List<int>, so strip a whitelist of real markup tags instead.
_HTML_TAG_RE = re.compile(
    r"</?(?:div|span|p|br|hr|b|i|em|strong|u|img|a|ul|ol|li|table|tr|td|th|"
    r"thead|tbody|h[1-6]|blockquote|pre|details|summary|sup|sub|small|"
    r"section|article|header|footer|nav|figure|figcaption)\b[^>]*/?>",
    re.IGNORECASE,
)

# Chat-UI noise lines that should be dropped entirely.
_UI_ARTIFACT_RE = re.compile(
    r"^\s*(?:copy code|copied!?|copy|retry|regenerate(?: response)?|share|"
    r"edited|thumbs up|thumbs down|stop generating|"
    r"ai-generated content may be incorrect\.?)\s*$",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def normalize_role(label: str) -> str | None:
    """Map a speaker label to 'human'/'assistant', or None if unrecognized."""
    key = label.strip().lower().rstrip(".")
    if key in HUMAN_LABELS:
        return "human"
    if key in ASSISTANT_LABELS:
        return "assistant"
    return None


def clean_content(text: str) -> str:
    """Strip HTML tags and UI artifacts; collapse excess blank lines."""
    lines = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)  # never touch code blocks
            continue
        if _UI_ARTIFACT_RE.match(line):
            continue
        lines.append(_HTML_TAG_RE.sub("", line))
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _match_speaker(line: str, in_fence: bool) -> tuple[str, str] | None:
    """If the line starts a new speaker turn, return (role, inline_content)."""
    if in_fence:
        return None
    m = _HEADING_SPEAKER_RE.match(line)
    if m:
        role = normalize_role(m.group("label"))
        if role:
            return role, ""
    m = _INLINE_SPEAKER_RE.match(line)
    if m and m.group("label").strip().lower().rstrip(".") in _ALL_LABELS:
        role = normalize_role(m.group("label"))
        if role:
            return role, m.group("rest")
    return None


def extract_turns(text: str) -> list:
    """Shared turn-detection engine, also used by the plaintext parser."""
    turns = []
    current_role = None
    current_lines: list[str] = []
    in_fence = False

    def flush():
        nonlocal current_lines
        if current_role is not None:
            content = clean_content("\n".join(current_lines))
            if content:
                turns.append(
                    {"role": current_role, "content": content, "timestamp": None}
                )
        current_lines = []

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        speaker = _match_speaker(line, in_fence and not _FENCE_RE.match(line))
        # A fence-toggle line is never a speaker line; re-check cleanly:
        if _FENCE_RE.match(line):
            speaker = None
        if speaker:
            flush()
            current_role, inline = speaker
            current_lines = [inline] if inline else []
        else:
            if current_role is None:
                # Preamble before the first detected speaker — assume human.
                current_role = "human"
            current_lines.append(line)
    flush()
    return turns


def parse(text: str) -> list:
    """Returns [(session_name, turns)]."""
    turns = extract_turns(text)
    if len(turns) < 2:
        log.warning(
            "Markdown parser detected %d turn(s) — speaker patterns may not "
            "have matched; content kept as-is.", len(turns),
        )
    log.info("Markdown parser extracted %d turn(s)", len(turns))
    return [(None, turns)]
