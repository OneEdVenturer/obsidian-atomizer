"""Writes atomic notes as .md files into the Obsidian vault.

- Filenames are kebab-cased from the note title.
- Notes are placed in subdirectories by domain: <output_dir>/<domain>/.
- Existing files are never overwritten — a -v2/-v3 suffix is appended.
- Source-tracking frontmatter fields are enforced deterministically here
  (the LLM's values for them are overridden) so provenance is always exact.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from extractor import SessionMeta
from splitter import Note

log = logging.getLogger("atomizer.writer")

VALID_TYPES = {
    "atomic-note", "decision", "insight", "open-question", "artifact",
    "prompt", "moc",
}
VALID_CONFIDENCE = {"high", "medium", "speculative"}
VALID_DOMAINS = {
    "structural-engineering", "software", "strategy", "brand", "lab-ops",
    "general",
}

# Canonical frontmatter key order for readable, diffable output.
_FIELD_ORDER = [
    "type", "source_session", "source_file", "source_format",
    "source_domain", "created", "confidence", "tags", "related", "status",
]


@dataclass
class WrittenNote:
    note: Note
    path: Path | None  # None in dry-run mode


def kebab_case(title: str, max_length: int = 80) -> str:
    """'DSM Lip Sweep Validation' -> 'dsm-lip-sweep-validation'."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "untitled-note"


def _normalize_frontmatter(note: Note, meta: SessionMeta) -> dict:
    """Validate, default, and override frontmatter fields; enforce order."""
    fm = dict(note.frontmatter)

    note_type = str(fm.get("type", "atomic-note")).strip().lower()
    if note_type not in VALID_TYPES:
        log.warning(
            "Note '%s': unknown type '%s' — defaulting to atomic-note.",
            note.title, note_type,
        )
        note_type = "atomic-note"

    confidence = str(fm.get("confidence", "medium")).strip().lower()
    if confidence not in VALID_CONFIDENCE:
        log.warning(
            "Note '%s': unknown confidence '%s' — defaulting to medium.",
            note.title, confidence,
        )
        confidence = "medium"

    domain = str(fm.get("source_domain", "general")).strip().lower()
    if domain not in VALID_DOMAINS:
        log.warning(
            "Note '%s': unrecognized domain '%s' — defaulting to general.",
            note.title, domain,
        )
        domain = "general"

    tags = fm.get("tags")
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    tags = [str(t).strip() for t in tags if str(t).strip()]

    related = fm.get("related")
    if not isinstance(related, list):
        related = [related] if related else []
    related = [str(r).strip() for r in related if str(r).strip()]

    ordered = {
        "type": note_type,
        "source_session": meta.session_name,
        "source_file": meta.source_file,
        "source_format": meta.source_format,
        "source_domain": domain,
        "created": meta.created,
        "confidence": confidence,
        "tags": tags,
        "related": related,
        "status": str(fm.get("status", "active")).strip().lower() or "active",
    }
    # Preserve any extra keys the LLM emitted, after the canonical ones.
    for key, value in fm.items():
        if key not in ordered and key not in ("title",):
            ordered[key] = value
    return ordered


def _versioned_path(directory: Path, slug: str) -> Path:
    """Return a non-existing path: slug.md, slug-v2.md, slug-v3.md, ..."""
    candidate = directory / f"{slug}.md"
    version = 2
    while candidate.exists():
        candidate = directory / f"{slug}-v{version}.md"
        version += 1
    return candidate


def write_notes(notes: list[Note], output_dir: Path, meta: SessionMeta,
                dry_run: bool = False) -> list[WrittenNote]:
    """Normalize frontmatter and write all notes. Returns written records."""
    written: list[WrittenNote] = []

    for note in notes:
        note.frontmatter = _normalize_frontmatter(note, meta)
        domain_dir = output_dir / note.frontmatter["source_domain"]

        if dry_run:
            print("\n" + "=" * 72)
            print(f"DRY RUN — would write: {domain_dir / (kebab_case(note.title) + '.md')}")
            print("=" * 72)
            print(note.render())
            written.append(WrittenNote(note=note, path=None))
            continue

        domain_dir.mkdir(parents=True, exist_ok=True)
        path = _versioned_path(domain_dir, kebab_case(note.title))
        path.write_text(note.render(), encoding="utf-8", newline="\n")
        if path.stem != kebab_case(note.title):
            log.info("File existed — wrote versioned copy: %s", path.name)
        log.info("Wrote %s (%s / %s)", path, note.note_type, note.domain)
        written.append(WrittenNote(note=note, path=path))

    return written
