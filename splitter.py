"""Splits raw LLM output into individual atomic notes.

The system prompt instructs the runtime LLM to emit each note as a complete
markdown document (YAML frontmatter + body) separated by ---ATOM_BREAK---
delimiter lines. This module splits on that delimiter and parses each block
into a Note (title, frontmatter dict, body).
"""

import logging
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("atomizer.splitter")

ATOM_BREAK = "---ATOM_BREAK---"

_BREAK_RE = re.compile(r"^\s*-{3,}\s*ATOM_BREAK\s*-{3,}\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Note:
    title: str
    frontmatter: dict = field(default_factory=dict)
    body: str = ""

    @property
    def note_type(self) -> str:
        return str(self.frontmatter.get("type", "atomic-note"))

    @property
    def domain(self) -> str:
        return str(self.frontmatter.get("source_domain", "general"))

    def render(self) -> str:
        """Render frontmatter + body as a markdown document."""
        fm = yaml.safe_dump(
            self.frontmatter, sort_keys=False, allow_unicode=True,
            default_flow_style=False, width=1000,
        ).strip()
        body = self.body.strip()
        if not body.lstrip().startswith("#"):
            body = f"# {self.title}\n\n{body}"
        return f"---\n{fm}\n---\n\n{body}\n"


def _strip_outer_fence(text: str) -> str:
    """Some models wrap the whole response in a single code fence."""
    stripped = text.strip()
    m = re.match(r"\A```[a-zA-Z]*\n(.*)\n```\s*\Z", stripped, re.DOTALL)
    return m.group(1) if m else text


def parse_note_block(block: str) -> Note | None:
    """Parse one delimited block into a Note. Returns None for empty/noise."""
    block = block.strip()
    if not block:
        return None

    frontmatter: dict = {}
    body = block
    m = _FRONTMATTER_RE.match(block)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                frontmatter = loaded
        except yaml.YAMLError as exc:
            log.warning("Unparseable YAML frontmatter in a note block: %s", exc)
        body = block[m.end():]

    body = body.strip()

    title = str(frontmatter.get("title") or "").strip()
    if not title:
        heading = _HEADING_RE.search(body)
        if heading:
            title = heading.group(1).strip()
    if not title:
        first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        title = first_line.lstrip("#").strip()[:80]
    if not title:
        log.warning("Discarding note block with no derivable title.")
        return None

    # Title lives in the filename and # heading; keep frontmatter clean.
    frontmatter.pop("title", None)
    return Note(title=title, frontmatter=frontmatter, body=body)


def split_llm_output(text: str) -> list[Note]:
    """Split a raw LLM response into Notes on ---ATOM_BREAK--- delimiters."""
    text = _strip_outer_fence(text)
    blocks = _BREAK_RE.split(text)
    notes = []
    for block in blocks:
        note = parse_note_block(block)
        if note:
            notes.append(note)
    log.info("Splitter parsed %d note(s) from LLM output (%d block(s))",
             len(notes), len(blocks))
    if not notes:
        log.warning(
            "No notes parsed from LLM output. First 500 chars of response: %s",
            text[:500],
        )
    return notes
