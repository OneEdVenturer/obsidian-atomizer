"""Vault indexing for cross-session linking.

Scans the configured vault directory once and builds an in-memory index of
every existing .md note — title, tags, source_session, body, and existing
wikilink targets — plus the distinctive concept phrases used for substring
matching. Pure filesystem + text parsing: no LLM call.

Kept separate from cross_linker.py so the scanning/parsing concern (this
module) is independent of the matching/injection concern (cross_linker.py).
Large vaults are read exactly once here; all matching then happens in memory.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from writer import kebab_case

log = logging.getLogger("atomizer.vault_index")

_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[\|#][^\]]*)?\]\]")
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9]|[A-Za-z0-9]")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over",
    "under", "between", "within", "their", "your", "about", "when", "what",
    "which", "while", "using", "used", "than", "then", "have", "has", "are",
    "was", "were", "will", "shall", "note", "notes", "via", "per",
}


def nfc(text: str) -> str:
    """Normalize to NFC so visually identical titles compare equal."""
    return unicodedata.normalize("NFC", text or "")


@dataclass
class VaultNote:
    path: Path
    title: str
    title_key: str = ""                     # NFC + lowercased, for comparison
    stem: str = ""
    tags: set = field(default_factory=set)  # lowercased
    source_session: str = ""
    note_type: str = "atomic-note"
    body_lower: str = ""
    link_targets: set = field(default_factory=set)  # lowercased existing links
    phrases: list = field(default_factory=list)     # concept-match phrases


# ---------------------------------------------------------------------------
# Frontmatter / link / tag parsing
# ---------------------------------------------------------------------------

def link_targets_from(body: str, frontmatter: dict) -> set:
    """All wikilink targets a note references (lowercased + kebab form)."""
    targets = set()

    def add(value: str) -> None:
        value = nfc(value).strip()
        if value:
            targets.add(value.lower())
            targets.add(kebab_case(value))

    for m in _WIKILINK_RE.finditer(body or ""):
        add(m.group(1))
    for field_name in ("related", "cross_links"):
        entries = frontmatter.get(field_name)
        if isinstance(entries, list):
            for entry in entries:
                m = _WIKILINK_RE.search(str(entry))
                add(m.group(1) if m else str(entry))
        elif entries:
            m = _WIKILINK_RE.search(str(entries))
            add(m.group(1) if m else str(entries))
    return targets


def tags_from(frontmatter: dict) -> set:
    tags = frontmatter.get("tags")
    if isinstance(tags, list):
        return {str(t).strip().lower() for t in tags if str(t).strip()}
    if tags:
        return {str(tags).strip().lower()}
    return set()


# ---------------------------------------------------------------------------
# Concept phrases
# ---------------------------------------------------------------------------

def _is_distinctive(token: str) -> bool:
    """A single token specific enough to anchor a concept match."""
    if "-" in token or any(c.isdigit() for c in token):
        return True
    return len(token) >= 7


def concept_phrases(title: str) -> list:
    """Distinctive phrases from a title for substring concept matching.

    Includes the full title, adjacent significant-word bigrams, and
    individually distinctive tokens (hyphenated, numeric, acronyms, or long
    words). Everything is NFC-normalized and lowercased; matching is
    word-boundary aware.
    """
    raw = nfc(title).strip()
    if not raw:
        return []
    tokens = _TOKEN_RE.findall(raw)
    significant = [
        t for t in tokens if len(t) >= 4 and t.lower() not in _STOPWORDS
    ]

    phrases = set()
    norm_title = raw.lower()
    if len(norm_title) >= 6:
        phrases.add(norm_title)
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS:
            continue
        if _is_distinctive(tok) or (tok.isupper() and len(tok) >= 2):
            phrases.add(low)
    for a, b in zip(significant, significant[1:]):
        phrases.add(f"{a.lower()} {b.lower()}")
    return [p for p in phrases if len(p) >= 4]


def phrase_in(phrase: str, haystack_lower: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
    return re.search(pattern, haystack_lower) is not None


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def parse_note_file(path: Path) -> VaultNote | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.debug("Skipping unreadable vault note %s: %s", path, exc)
        return None

    frontmatter: dict = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                frontmatter = loaded
        except yaml.YAMLError:
            frontmatter = {}
        body = text[m.end():]

    title = nfc(str(frontmatter.get("title") or "")).strip()
    if not title:
        heading = _HEADING_RE.search(body)
        title = nfc(heading.group(1)).strip() if heading else path.stem
    if not title:
        return None

    return VaultNote(
        path=path,
        title=title,
        title_key=title.lower(),
        stem=path.stem,
        tags=tags_from(frontmatter),
        source_session=str(frontmatter.get("source_session") or "").strip(),
        note_type=str(frontmatter.get("type") or "atomic-note").strip().lower(),
        body_lower=body.lower(),
        link_targets=link_targets_from(body, frontmatter),
        phrases=concept_phrases(title),
    )


def index_vault(vault_dir: Path) -> list:
    """Scan the vault recursively and index every .md note (no LLM call)."""
    if not vault_dir.is_dir():
        log.info("Vault directory %s does not exist yet — no existing notes "
                 "to cross-link against.", vault_dir)
        return []
    index = []
    for path in sorted(vault_dir.rglob("*.md")):
        note = parse_note_file(path)
        if note:
            index.append(note)
    log.info("Cross-link index: %d existing vault note(s) under %s",
             len(index), vault_dir)
    return index
