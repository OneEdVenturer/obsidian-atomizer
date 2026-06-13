"""Post-pass wikilink validation.

After all notes are written, every [[wikilink]] (in note bodies and in the
`related` frontmatter list) is checked against the titles of the notes in
the output batch. Links that don't resolve are reported as warnings —
they may legitimately point at notes elsewhere in the vault, so they are
flagged, never deleted.
"""

import logging
import re
from dataclasses import dataclass

from writer import WrittenNote, kebab_case

log = logging.getLogger("atomizer.linker")

_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[\|#][^\]]*)?\]\]")


@dataclass
class DanglingLink:
    source_title: str
    target: str


def _links_in_note(written: WrittenNote) -> set:
    targets = set()
    for m in _WIKILINK_RE.finditer(written.note.body):
        targets.add(m.group(1).strip())
    related = written.note.frontmatter.get("related", [])
    if isinstance(related, list):
        for entry in related:
            m = _WIKILINK_RE.search(str(entry))
            if m:
                targets.add(m.group(1).strip())
            else:
                text = str(entry).strip()
                if text:
                    targets.add(text)
    return targets


def _cross_link_targets(written: WrittenNote) -> set:
    """Targets injected by cross-session linking — valid by construction.

    These point at existing notes elsewhere in the vault (not this batch),
    so they must not be reported as dangling.
    """
    exempt = set()
    cross = written.note.frontmatter.get("cross_links", [])
    if isinstance(cross, list):
        for entry in cross:
            m = _WIKILINK_RE.search(str(entry))
            target = (m.group(1) if m else str(entry)).strip()
            if target:
                exempt.add(target.lower())
                exempt.add(kebab_case(target))
    return exempt


def validate_links(written_notes: list) -> list:
    """Check that all wikilinks resolve to notes in this batch.

    Matching is case-insensitive against both note titles and their
    kebab-case filenames (Obsidian resolves either). Cross-session links
    injected into the `cross_links` frontmatter are exempt — they resolve
    to existing vault notes outside this batch by construction.
    """
    known = set()
    for w in written_notes:
        known.add(w.note.title.lower())
        known.add(kebab_case(w.note.title))

    dangling: list[DanglingLink] = []
    for w in written_notes:
        exempt = _cross_link_targets(w)
        for target in sorted(_links_in_note(w)):
            key = target.lower()
            if key in exempt or kebab_case(target) in exempt:
                continue  # cross-session link to an existing vault note
            if key not in known and kebab_case(target) not in known:
                dangling.append(DanglingLink(source_title=w.note.title,
                                             target=target))
                log.warning(
                    "Dangling wikilink: note '%s' links to '[[%s]]' which "
                    "is not in this output batch.", w.note.title, target,
                )

    if not dangling:
        log.info("Wikilink validation: all links resolve within the batch.")
    else:
        log.info("Wikilink validation: %d dangling link(s) found.",
                 len(dangling))
    return dangling
