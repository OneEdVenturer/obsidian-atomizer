"""Chunking for oversized conversations, plus post-chunk merge/dedup.

If the normalized conversation exceeds chunk_threshold_ratio (default 0.8)
of the runtime model's context window, it is split into overlapping chunks
at turn boundaries (never mid-message), each chunk sharing a 10-turn overlap
with its predecessor. After all chunks are processed, the resulting note
sets are merged: notes whose titles are more than dedupe_similarity (default
0.85) similar are treated as duplicates, and wikilinks pointing at dropped
duplicates are re-resolved to the surviving note titles.

Token counts are local estimates (tiktoken cl100k_base with a 1.15 safety
multiplier — Claude's tokenizer differs from tiktoken's, so we deliberately
overestimate). They are used only for the chunking decision; billing numbers
come from the API's usage fields.
"""

import difflib
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("atomizer.chunker")

_SAFETY_MULTIPLIER = 1.15

try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # tiktoken missing or model files unavailable offline
    _ENCODER = None


def estimate_tokens(text: str) -> int:
    """Approximate token count for chunking decisions."""
    if _ENCODER is not None:
        raw = len(_ENCODER.encode(text, disallowed_special=()))
    else:
        raw = max(1, len(text) // 4)
    return int(raw * _SAFETY_MULTIPLIER)


def conversation_tokens(turns: list) -> int:
    return sum(estimate_tokens(t["content"]) + 8 for t in turns)


@dataclass
class ChunkPlan:
    chunks: list = field(default_factory=list)  # list[list[turn]]

    @property
    def count(self) -> int:
        return len(self.chunks)


def plan_chunks(turns: list, context_window: int, threshold_ratio: float = 0.8,
                overlap_turns: int = 10, reserved_tokens: int = 24000) -> ChunkPlan:
    """Split turns into overlapping chunks if the conversation is too large.

    reserved_tokens accounts for the system prompt and output headroom so a
    chunk plus its scaffolding stays inside the context window.
    """
    total = conversation_tokens(turns)
    limit = int(context_window * threshold_ratio)

    if total <= limit:
        log.info(
            "Conversation is ~%d tokens (limit %d) — no chunking needed.",
            total, limit,
        )
        return ChunkPlan(chunks=[turns])

    budget = max(limit - reserved_tokens, 10000)
    log.info(
        "Conversation is ~%d tokens, exceeding %d (%.0f%% of %d-token "
        "context window) — chunking at turn boundaries with %d-turn overlap.",
        total, limit, threshold_ratio * 100, context_window, overlap_turns,
    )

    chunks = []
    start = 0
    n = len(turns)
    while start < n:
        used = 0
        end = start
        while end < n:
            cost = estimate_tokens(turns[end]["content"]) + 8
            if used + cost > budget and end > start:
                break
            if cost > budget and end == start:
                log.warning(
                    "Turn %d alone (~%d tokens) exceeds the chunk budget "
                    "(%d); including it as a single-turn chunk.",
                    end, cost, budget,
                )
                end += 1
                used += cost
                break
            used += cost
            end += 1
        chunks.append(turns[start:end])
        if end >= n:
            break
        # Next chunk starts `overlap_turns` before this chunk's end, but must
        # always advance to guarantee termination.
        start = max(end - overlap_turns, start + 1)

    log.info("Chunk plan: %d chunk(s) of sizes %s",
             len(chunks), [len(c) for c in chunks])
    return ChunkPlan(chunks=chunks)


# ---------------------------------------------------------------------------
# Merge / dedup across chunk results
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(\|[^\]]*)?\]\]")


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _rewrite_wikilinks(text: str, title_map: dict) -> str:
    """Repoint [[links]] whose targets were deduped away."""
    def repl(m):
        target = m.group(1).strip()
        alias = m.group(2) or ""
        replacement = title_map.get(target.lower())
        if replacement and replacement.lower() != target.lower():
            return f"[[{replacement}{alias}]]"
        return m.group(0)
    return _WIKILINK_RE.sub(repl, text)


def merge_chunk_results(note_sets: list, threshold: float = 0.85):
    """Merge per-chunk note lists into one deduplicated list.

    Returns (merged_notes, duplicates_found). Duplicate = title similarity
    above `threshold` against an already-kept note; the first occurrence
    wins. Wikilinks across the merged set are re-resolved so links to a
    dropped duplicate point at the surviving note.
    """
    kept = []
    title_map: dict = {}  # lowercase dropped/kept title -> kept title
    duplicates = 0

    for chunk_idx, notes in enumerate(note_sets):
        for note in notes:
            match = None
            for existing in kept:
                score = _similarity(note.title, existing.title)
                if score >= threshold:
                    match = (existing, score)
                    break
            if match:
                existing, score = match
                duplicates += 1
                title_map[note.title.lower()] = existing.title
                log.info(
                    "Merge: dropping duplicate note '%s' from chunk %d "
                    "(%.0f%% title match with '%s').",
                    note.title, chunk_idx + 1, score * 100, existing.title,
                )
            else:
                title_map[note.title.lower()] = note.title
                kept.append(note)

    # Re-resolve wikilinks across the merged set.
    for note in kept:
        note.body = _rewrite_wikilinks(note.body, title_map)
        related = note.frontmatter.get("related")
        if isinstance(related, list):
            resolved = []
            for entry in related:
                entry_str = _rewrite_wikilinks(str(entry), title_map)
                if entry_str not in resolved:
                    resolved.append(entry_str)
            note.frontmatter["related"] = resolved

    if len(note_sets) > 1:
        log.info(
            "Merge complete: %d chunk result sets -> %d unique note(s), "
            "%d duplicate(s) removed.",
            len(note_sets), len(kept), duplicates,
        )
    return kept, duplicates
