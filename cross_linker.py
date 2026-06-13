"""Cross-session wikilink resolution: matching, injection, backup/restore.

Builds on vault_index.py (which scans and indexes the vault) to connect a
batch of new notes to related older notes across the whole vault.

Two entry points:

- link_against_vault(...) — normal mode. New notes (in memory, not yet
  written) are matched against the existing vault; forward links are
  injected into the new notes and reciprocal backlinks are written into the
  matched old notes on disk.
- relink_vault(...) — `--cross-link-only` mode. Every note already on disk
  is treated as both source and target and re-linked in place.

Backlink injection into existing files is the delicate part: each target is
backed up to a `.bak` first (configurable), the edit is written, then the
YAML frontmatter is re-parsed to confirm it survived — on any corruption the
original is restored from the backup and the failure is logged.

Matching strategies (any one is a match):
    A. Title similarity   — difflib ratio >= title_similarity_threshold
    B. Tag overlap        — shared tag count >= min_shared_tags
    C. Concept match      — a distinctive phrase from one note's title
                            appears in the other note's body (either way)

Candidates are filtered (self, same source_session, MOCs, already-linked),
ranked title > tag > concept, capped per note, and rendered with a short
"matched via ..." reason in the footer.
"""

import difflib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vault_index import (
    VaultNote,
    _FRONTMATTER_RE,
    concept_phrases,
    index_vault,
    link_targets_from,
    nfc,
    phrase_in,
    tags_from,
)
from writer import kebab_case

log = logging.getLogger("atomizer.cross_linker")

FOOTER_HEADING = "## Related (Cross-Session)"

DEFAULT_TITLE_SIMILARITY = 0.65
DEFAULT_MIN_SHARED_TAGS = 3
DEFAULT_MAX_LINKS = 5
DEFAULT_BACKUP = True
DEFAULT_BACKUP_EXT = ".bak"

_PRIORITY = {"title": 3, "tag": 2, "concept": 1}


# ---------------------------------------------------------------------------
# Settings + query helpers
# ---------------------------------------------------------------------------

@dataclass
class LinkSettings:
    enabled: bool = True
    title_threshold: float = DEFAULT_TITLE_SIMILARITY
    min_shared_tags: int = DEFAULT_MIN_SHARED_TAGS
    max_links: int = DEFAULT_MAX_LINKS
    backup: bool = DEFAULT_BACKUP
    backup_ext: str = DEFAULT_BACKUP_EXT

    @classmethod
    def from_config(cls, config: dict) -> "LinkSettings":
        s = (config or {}).get("cross_session_linking", {}) or {}
        # Accept the current key names and the earlier ones as fallbacks.
        return cls(
            enabled=bool(s.get("enabled", True)),
            title_threshold=float(s.get("title_similarity_threshold",
                                        DEFAULT_TITLE_SIMILARITY)),
            min_shared_tags=int(s.get("min_shared_tags",
                                      s.get("tag_overlap_threshold",
                                            DEFAULT_MIN_SHARED_TAGS))),
            max_links=int(s.get("max_cross_links_per_note",
                                s.get("max_links_per_note", DEFAULT_MAX_LINKS))),
            backup=bool(s.get("backup_before_backlink", DEFAULT_BACKUP)),
            backup_ext=str(s.get("backup_extension", DEFAULT_BACKUP_EXT)),
        )


@dataclass
class Query:
    """The fields needed to match one source note against the vault index."""
    title: str
    title_key: str
    tags: set
    phrases: list
    body_lower: str
    link_targets: set
    source_session: str
    self_path: Path | None = None


def query_from_note(note, current_session: str) -> Query:
    """Build a Query from a splitter.Note (new, in-memory note)."""
    title = nfc(note.title).strip()
    return Query(
        title=title,
        title_key=title.lower(),
        tags=tags_from(note.frontmatter),
        phrases=concept_phrases(title),
        body_lower=(note.body or "").lower(),
        link_targets=link_targets_from(note.body, note.frontmatter),
        source_session=current_session,
        self_path=None,
    )


def query_from_vault_note(vnote: VaultNote) -> Query:
    """Build a Query from an existing VaultNote (cross-link-only mode)."""
    return Query(
        title=vnote.title,
        title_key=vnote.title_key,
        tags=vnote.tags,
        phrases=vnote.phrases,
        body_lower=vnote.body_lower,
        link_targets=vnote.link_targets,
        source_session=vnote.source_session,
        self_path=vnote.path,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    vault_note: VaultNote
    strategies: list
    score: float
    reason: str          # footer text: "matched via ..."
    score_desc: str      # dry-run text: "title: 0.71" / "tags: 3 shared"


def find_candidates(query: Query, index: list,
                    settings: LinkSettings) -> list:
    """Cross-session link candidates for one source note, filtered + ranked."""
    candidates = []
    for vnote in index:
        # --- Filters ---
        if vnote.note_type == "moc":
            continue
        if query.self_path is not None and vnote.path == query.self_path:
            continue  # self
        if (vnote.source_session and query.source_session
                and vnote.source_session == query.source_session):
            continue  # same session/batch — intra-batch linking owns this
        if vnote.title_key == query.title_key and query.self_path is None:
            continue  # identical title (new-note side): avoid self-reference
        if (vnote.title_key in query.link_targets
                or vnote.stem.lower() in query.link_targets
                or kebab_case(vnote.title) in query.link_targets):
            continue  # link already present

        best_priority = 0
        score = 0.0
        strategies = []
        reason = ""
        score_desc = ""

        # Strategy A: title similarity.
        ratio = difflib.SequenceMatcher(
            None, query.title_key, vnote.title_key).ratio()
        if ratio >= settings.title_threshold:
            strategies.append("title")
            if _PRIORITY["title"] > best_priority:
                best_priority = _PRIORITY["title"]
                reason = f"matched via title similarity ({ratio:.2f})"
                score_desc = f"title: {ratio:.2f}"
            score = max(score, _PRIORITY["title"] * 1000 + ratio * 100)

        # Strategy B: tag overlap.
        shared = sorted(query.tags & vnote.tags)
        if len(shared) >= settings.min_shared_tags:
            strategies.append("tag")
            if _PRIORITY["tag"] > best_priority:
                best_priority = _PRIORITY["tag"]
                reason = "matched via shared tags: " + ", ".join(shared)
                score_desc = f"tags: {len(shared)} shared"
            score = max(score, _PRIORITY["tag"] * 1000 + len(shared) * 10)

        # Strategy C: explicit concept match (substring, either direction).
        hits = 0
        for phrase in query.phrases:
            if phrase_in(phrase, vnote.body_lower):
                hits += 1
        for phrase in vnote.phrases:
            if phrase_in(phrase, query.body_lower):
                hits += 1
        if hits > 0:
            strategies.append("concept")
            if _PRIORITY["concept"] > best_priority:
                best_priority = _PRIORITY["concept"]
                reason = "matched via concept overlap"
                score_desc = "concept"
            score = max(score, _PRIORITY["concept"] * 1000 + hits)

        if strategies:
            candidates.append(
                Candidate(vnote, strategies, score, reason, score_desc))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:settings.max_links]


# ---------------------------------------------------------------------------
# Footer rendering
# ---------------------------------------------------------------------------

def _bullet(title: str, reason: str) -> str:
    return f"- [[{title}]] — {reason}"


def _append_footer(body: str, bullets: list) -> str:
    body = (body or "").rstrip()
    block = "\n".join(bullets)
    if FOOTER_HEADING in body:
        return body + "\n" + block + "\n"
    return body + "\n\n" + FOOTER_HEADING + "\n\n" + block + "\n"


# ---------------------------------------------------------------------------
# Forward links (into a new, in-memory note)
# ---------------------------------------------------------------------------

def inject_forward_links(note, candidates: list) -> list:
    """Add cross_links frontmatter + footer to a new note.

    Returns the candidates actually added (after de-duplication).
    """
    already = link_targets_from(note.body, note.frontmatter)
    added = []
    for cand in candidates:
        t = cand.vault_note.title
        if t.lower() in already or kebab_case(t) in already:
            continue
        added.append(cand)
        already.add(t.lower())
        already.add(kebab_case(t))
    if not added:
        return []

    existing = note.frontmatter.get("cross_links")
    existing = list(existing) if isinstance(existing, list) else []
    note.frontmatter["cross_links"] = existing + [
        f"[[{c.vault_note.title}]]" for c in added]
    note.body = _append_footer(
        note.body, [_bullet(c.vault_note.title, c.reason) for c in added])
    log.info("Forward links for '%s' -> %s",
             note.title, ", ".join(c.vault_note.title for c in added))
    return added


# ---------------------------------------------------------------------------
# Backlinks (into an existing file on disk) — the delicate part
# ---------------------------------------------------------------------------

@dataclass
class BacklinkResult:
    added: int = 0
    backup_created: bool = False
    failed: bool = False


def _frontmatter_parses(text: str) -> bool:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    try:
        return isinstance(yaml.safe_load(m.group(1)), dict)
    except yaml.YAMLError:
        return False


def inject_backlinks_into_file(path: Path, new_links: dict,
                               settings: LinkSettings) -> BacklinkResult:
    """Add reciprocal backlinks to an existing note, safely.

    new_links maps {new_note_title: reason}. The original is backed up to a
    `.bak` first (if configured); after writing, the YAML frontmatter is
    re-parsed and the original restored on any corruption.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.warning("Could not read %s to add backlinks: %s", path, exc)
        return BacklinkResult(failed=True)

    m = _FRONTMATTER_RE.match(text)
    if not m:
        log.warning("Skipping backlinks for %s — no YAML frontmatter.", path)
        return BacklinkResult(failed=True)
    try:
        loaded = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        log.warning("Skipping backlinks for %s — unparseable frontmatter.",
                    path)
        return BacklinkResult(failed=True)
    frontmatter = loaded if isinstance(loaded, dict) else {}
    body = text[m.end():]

    existing = link_targets_from(body, frontmatter)
    to_add = [
        (t, r) for t, r in sorted(new_links.items())
        if t.lower() not in existing and kebab_case(t) not in existing
    ]
    if not to_add:
        return BacklinkResult(added=0)

    current = frontmatter.get("cross_links")
    current = list(current) if isinstance(current, list) else []
    frontmatter["cross_links"] = current + [f"[[{t}]]" for t, _ in to_add]
    new_body = _append_footer(body, [_bullet(t, r) for t, r in to_add])

    fm_yaml = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True,
        default_flow_style=False, width=1000,
    ).strip()
    new_text = f"---\n{fm_yaml}\n---\n\n{new_body.strip()}\n"

    # --- Backup, write, verify, restore-on-failure ---
    backup_path = Path(str(path) + settings.backup_ext)
    backup_created = False
    if settings.backup:
        try:
            shutil.copy2(path, backup_path)
            backup_created = True
        except OSError as exc:
            log.warning("Could not create backup %s: %s — proceeding "
                        "without backup.", backup_path, exc)

    try:
        path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        log.error("❌ Backlink injection failed for %s — write error: %s",
                  path.name, exc)
        if backup_created:
            shutil.copy2(backup_path, path)
            log.info("Restored %s from backup.", path.name)
        return BacklinkResult(backup_created=backup_created, failed=True)

    # Verify the YAML frontmatter still parses; restore if not.
    verify = path.read_text(encoding="utf-8-sig", errors="replace")
    if not _frontmatter_parses(verify):
        log.error("❌ Backlink injection failed for %s — restored from backup",
                  path.name)
        if backup_created:
            shutil.copy2(backup_path, path)
        return BacklinkResult(backup_created=backup_created, failed=True)

    log.info("Backlinks into %s: %s",
             path.name, ", ".join(t for t, _ in to_add))
    return BacklinkResult(added=len(to_add), backup_created=backup_created)


# ---------------------------------------------------------------------------
# Stats + preview
# ---------------------------------------------------------------------------

def empty_stats() -> dict:
    return {"enabled": False, "forward": 0, "forward_notes": 0,
            "backlinks": 0, "backlink_notes": 0, "failures": 0,
            "backups": 0, "preview": []}


def format_stats_block(stats: dict) -> list:
    """Render the multi-line cross-session summary block (value column 24)."""
    fwd_label = stats.get("forward_label", "New → Old")
    back_label = stats.get("backlink_label", "Old → New")

    def row(label: str, value: str) -> str:
        # Visual width counts the arrow as one cell, matching len().
        return label.ljust(24) + value

    return [
        row("Cross-session links:",
            f"{stats['forward']} forward, {stats['backlinks']} backlinks"),
        row(f"  {fwd_label}:",
            f"{stats['forward']} links across {stats['forward_notes']} notes"),
        row(f"  {back_label}:",
            f"{stats['backlinks']} backlinks injected into "
            f"{stats['backlink_notes']} existing notes"),
        row("  Backlink failures:", str(stats["failures"])),
        row("  Backup files created:",
            f"{stats['backups']} ({_backup_ext_for(stats)})"),
    ]


def _backup_ext_for(stats: dict) -> str:
    return stats.get("backup_ext", DEFAULT_BACKUP_EXT)


def format_preview(preview: list) -> list:
    """Render the CROSS-SESSION LINK PREVIEW block for --dry-run."""
    lines = ["=" * 72, "CROSS-SESSION LINK PREVIEW", "=" * 72]
    if not preview:
        lines.append("No cross-session matches found.")
        lines.append("=" * 72)
        return lines
    for entry in preview:
        lines.append(f'NEW: "{entry["title"]}"')
        for tgt, desc in entry["links"]:
            lines.append(f"  → would link to: [[{tgt}]] ({desc})")
        if entry["links"]:
            lines.append(
                "  ← would backlink into: "
                + ", ".join(f"[[{t}]]" for t, _ in entry["links"]))
        else:
            lines.append("  (no cross-session matches found)")
        lines.append("")
    lines.append("=" * 72)
    return lines


# ---------------------------------------------------------------------------
# Orchestration: normal mode
# ---------------------------------------------------------------------------

def link_against_vault(new_notes: list, vault_dir: Path, current_session: str,
                       config: dict, dry_run: bool = False) -> dict:
    """Resolve cross-session links for a batch of new (in-memory) notes."""
    settings = LinkSettings.from_config(config)
    stats = empty_stats()
    stats["backup_ext"] = settings.backup_ext
    if not settings.enabled:
        log.info("Cross-session linking disabled in config.")
        return stats
    stats["enabled"] = True

    index = index_vault(vault_dir)
    if not index:
        return stats

    backlink_map: dict = {}  # vault path -> {new_title: reason}
    for note in new_notes:
        if note.note_type == "moc":
            continue
        query = query_from_note(note, current_session)
        candidates = find_candidates(query, index, settings)
        if not candidates:
            continue
        added = inject_forward_links(note, candidates)
        if not added:
            continue
        stats["forward"] += len(added)
        stats["forward_notes"] += 1
        stats["preview"].append({
            "title": note.title,
            "links": [(c.vault_note.title, c.score_desc) for c in added],
        })
        for cand in added:
            backlink_map.setdefault(cand.vault_note.path, {})[note.title] = \
                cand.reason

    if dry_run:
        stats["backlinks"] = sum(len(v) for v in backlink_map.values())
        stats["backlink_notes"] = len(backlink_map)
        log.info("Cross-session linking (dry-run): %d forward link(s) across "
                 "%d note(s); %d backlink(s) into %d note(s) would be written.",
                 stats["forward"], stats["forward_notes"], stats["backlinks"],
                 stats["backlink_notes"])
        return stats

    for path, links in backlink_map.items():
        result = inject_backlinks_into_file(path, links, settings)
        stats["backlinks"] += result.added
        if result.added:
            stats["backlink_notes"] += 1
        if result.backup_created:
            stats["backups"] += 1
        if result.failed:
            stats["failures"] += 1

    log.info("Cross-session linking: %d forward link(s) across %d note(s), "
             "%d backlink(s) into %d existing note(s), %d failure(s).",
             stats["forward"], stats["forward_notes"], stats["backlinks"],
             stats["backlink_notes"], stats["failures"])
    return stats


# ---------------------------------------------------------------------------
# Orchestration: --cross-link-only (vault-wide re-link)
# ---------------------------------------------------------------------------

def relink_vault(vault_dir: Path, config: dict, dry_run: bool = False) -> dict:
    """Re-link the whole vault in place: every note is source AND target.

    Each note is matched against all others; both the note and each matched
    partner receive a reciprocal link. De-duplication (via the in-memory
    link index, updated as we go) prevents a pair from being linked twice.
    """
    settings = LinkSettings.from_config(config)
    stats = empty_stats()
    stats["backup_ext"] = settings.backup_ext
    stats["forward_label"] = "Source → Match"
    stats["backlink_label"] = "Match → Source"
    if not settings.enabled:
        log.info("Cross-session linking disabled in config.")
        stats["enabled"] = True
        return stats
    stats["enabled"] = True

    index = index_vault(vault_dir)
    atoms = [n for n in index if n.note_type != "moc"]
    if len(atoms) < 2:
        log.info("Vault has fewer than 2 atomic notes — nothing to relink.")
        return stats

    by_path = {n.path: n for n in index}
    # Pending links to write per file: {path: {partner_title: reason}}.
    pending: dict = {p: {} for p in by_path}
    linked_pairs = set()  # frozenset({pathA, pathB}) already connected

    for src in atoms:
        query = query_from_vault_note(src)
        candidates = find_candidates(query, index, settings)
        for cand in candidates:
            tgt = cand.vault_note
            pair = frozenset((src.path, tgt.path))
            if pair in linked_pairs:
                continue
            linked_pairs.add(pair)
            # Reciprocal: source gains link to target, target to source.
            pending[src.path][tgt.title] = cand.reason
            pending[tgt.path][src.title] = cand.reason
            # Keep the in-memory index honest for later dedup this run.
            src.link_targets.add(tgt.title_key)
            tgt.link_targets.add(src.title_key)

    # First note of each pair counts as "forward", the partner as "backlink".
    stats["forward"] = sum(1 for _ in linked_pairs)
    stats["backlinks"] = stats["forward"]
    stats["forward_notes"] = sum(1 for p, v in pending.items() if v)

    if dry_run:
        for src in atoms:
            links = pending[src.path]
            if links:
                stats["preview"].append({
                    "title": src.title,
                    "links": [(t, "relink") for t in sorted(links)],
                })
        stats["backlink_notes"] = stats["forward_notes"]
        log.info("Cross-link-only (dry-run): %d pair(s) across %d note(s) "
                 "would be linked.", len(linked_pairs), stats["forward_notes"])
        return stats

    written_notes = 0
    for path, links in pending.items():
        if not links:
            continue
        result = inject_backlinks_into_file(path, links, settings)
        if result.added:
            written_notes += 1
        if result.backup_created:
            stats["backups"] += 1
        if result.failed:
            stats["failures"] += 1
    stats["forward_notes"] = written_notes
    stats["backlink_notes"] = written_notes
    log.info("Cross-link-only: %d pair(s) linked across %d note(s), "
             "%d failure(s).", len(linked_pairs), written_notes,
             stats["failures"])
    return stats
