"""Microsoft Copilot / M365 chat copy-paste parser (.md or .txt).

Detects speaker turns by Copilot-specific patterns ("You said:",
"Copilot said:", "You", "Copilot", "Bing", "Microsoft 365 Copilot") and
strips Copilot UI artifacts: citation footnotes, "Learn more" link blocks,
reference superscripts like [1], and disclaimer lines.
"""

import logging
import re

from parsers.markdown_parser import clean_content

log = logging.getLogger("atomizer.parsers.copilot")

_COPILOT_ASSISTANT = {
    "copilot", "bing", "microsoft copilot", "microsoft 365 copilot",
    "m365 copilot", "copilot said", "bing said",
}
_COPILOT_HUMAN = {"you", "me", "you said", "user", "user said"}

# "You said:", "**Copilot**", "Copilot said", "## You", optionally bold.
_SPEAKER_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:[*_]{1,2})?"
    r"(?P<label>You said|You|Me|User said|User|Copilot said|Copilot|"
    r"Bing said|Bing|Microsoft 365 Copilot|M365 Copilot|Microsoft Copilot)"
    r"(?:[*_]{1,2})?\s*[:：]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

# Footnote-style citations: "[1]: https://..." and inline refs like "[2]".
_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\d+\][:.]?\s+\S+.*$")
_INLINE_REF_RE = re.compile(r"\s*[\[⁽]\d+[\]⁾]")
_SUPERSCRIPT_REF_RE = re.compile(r"[¹²³⁰⁴-⁹]+")

_NOISE_LINE_RE = re.compile(
    r"^\s*(?:learn more:?|sources?:?|references?:?|"
    r"ai-generated content may be incorrect\.?|"
    r"copilot uses ai\. check for mistakes\.?|"
    r"\d+\s+of\s+\d+\s+responses?)\s*$",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def matches(text: str) -> bool:
    """Content sniff used by format auto-detection."""
    head = "\n".join(text.splitlines()[:200])
    copilot_hits = len(re.findall(
        r"^\s*(?:#{1,6}\s+)?(?:[*_]{1,2})?(?:Copilot(?:\s+said)?|Bing|"
        r"Microsoft 365 Copilot|Microsoft Copilot)(?:[*_]{1,2})?\s*[:：]?\s*",
        head, re.IGNORECASE | re.MULTILINE,
    ))
    return copilot_hits >= 1


def _strip_copilot_noise(text: str) -> str:
    lines = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        if _NOISE_LINE_RE.match(line) or _FOOTNOTE_DEF_RE.match(line):
            continue
        line = _INLINE_REF_RE.sub("", line)
        line = _SUPERSCRIPT_REF_RE.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def _role_for(label: str) -> str | None:
    key = label.strip().lower()
    if key in _COPILOT_ASSISTANT:
        return "assistant"
    if key in _COPILOT_HUMAN:
        return "human"
    return None


def parse(text: str) -> list:
    """Returns [(session_name, turns)]."""
    turns = []
    current_role = None
    current_lines: list[str] = []
    in_fence = False

    def flush():
        nonlocal current_lines
        if current_role is not None:
            content = clean_content(_strip_copilot_noise("\n".join(current_lines)))
            if content:
                turns.append(
                    {"role": current_role, "content": content, "timestamp": None}
                )
        current_lines = []

    for line in text.splitlines():
        is_fence = bool(_FENCE_RE.match(line))
        if is_fence:
            in_fence = not in_fence
        m = None if (in_fence or is_fence) else _SPEAKER_RE.match(line)
        role = _role_for(m.group("label")) if m else None
        if role:
            flush()
            current_role = role
            rest = m.group("rest").strip()
            current_lines = [rest] if rest else []
        else:
            if current_role is None:
                current_role = "human"
            current_lines.append(line)
    flush()

    log.info("Copilot parser extracted %d turn(s)", len(turns))
    return [(None, turns)]
