#!/usr/bin/env python3
"""obsidian-atomizer — turn raw AI conversation threads into linked,
tagged, frontmattered atomic Zettelkasten notes in an Obsidian vault.

Usage examples:
    python atomizer.py --input ./my-chat-session.md
    python atomizer.py --input ./claude-export.json --format claude-json
    python atomizer.py --input ./conversations.json --format chatgpt-json
    python atomizer.py --input ./chat.md --session "RackCalc-DSM-Session"
    python atomizer.py --input ./chat.md --provider local
    python atomizer.py --input ./chat.md --dry-run
    python atomizer.py --input ./chat.md --parse-only
    python atomizer.py --input-dir ./exports/ --format auto
"""

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import yaml

import archiver
import chunker
import cross_linker
import extractor
import linker
import llm_client
import manifest as manifest_mod
import optimizer
import writer
from parsers import FORMATS, Conversation, parse_file, pdf_parser
from splitter import Note

log = logging.getLogger("atomizer")

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"
INPUT_EXTENSIONS = {".md", ".txt", ".json", ".pdf", ".docx"}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="atomizer",
        description="Extract atomic Zettelkasten notes from AI conversation "
                    "exports into an Obsidian vault.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input", type=Path,
                     help="Path to a single conversation file (.md/.txt/.json).")
    src.add_argument("--input-dir", type=Path,
                     help="Batch mode: process every .md/.txt/.json file in "
                          "this directory.")
    p.add_argument("--format", default="auto",
                   choices=("auto",) + FORMATS,
                   help="Input format (default: auto-detect).")
    p.add_argument("--session",
                   help="Session name override (default: conversation title "
                        "or input filename).")
    p.add_argument("--provider", choices=("anthropic", "local"),
                   help="LLM provider override (default: from config.yaml).")
    p.add_argument("--model",
                   help="Model ID override (default: from config.yaml).")
    p.add_argument("--output-dir", type=Path,
                   help="Vault output directory override.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="Path to config.yaml (default: alongside atomizer.py).")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview notes on the console; write no files.")
    p.add_argument("--parse-only", action="store_true",
                   help="Print the normalized conversation as JSON and exit "
                        "without calling the LLM (parser testing).")
    p.add_argument("--force", action="store_true",
                   help="Reprocess inputs even if their content hash is "
                        "already in the manifest.")
    p.add_argument("--status", action="store_true",
                   help="Print a summary of all previously processed files "
                        "and exit.")
    p.add_argument("--no-optimize", action="store_true",
                   help="Skip the pre-send token optimization pipeline.")
    p.add_argument("--optimize-only", action="store_true",
                   help="Parse + optimize and print the report and cleaned "
                        "text; do not call the extraction LLM.")
    p.add_argument("--optimize-mode", choices=("rules", "llm", "both"),
                   help="Override optimization mode for this run.")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip auto-archiving; leave input files in place.")
    p.add_argument("--no-cross-link", "--no-cross-links", dest="no_cross_link",
                   action="store_true",
                   help="Skip cross-session wikilink resolution against the "
                        "existing vault.")
    p.add_argument("--cross-link-only", action="store_true",
                   help="Skip LLM extraction entirely and re-link the current "
                        "vault in place (every note is source and target).")
    p.add_argument("--yes", action="store_true",
                   help="Auto-confirm prompts (e.g. image-heavy PDF "
                        "warnings) for scripted/batch use.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug-level logging.")
    return p


def setup_logging(verbose: bool) -> None:
    # Windows consoles may default to cp1252; never crash on Unicode output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"Config file {path} must contain a YAML mapping.")
    return config


def resolve_output_dir(args, config: dict) -> Path:
    output_dir = (
        args.output_dir
        or Path(str(config.get("output_dir", "~/ObsidianVault/Atoms")))
    )
    return Path(output_dir).expanduser()


def gather_inputs(args) -> list:
    if args.input:
        if not args.input.exists():
            raise SystemExit(f"Input file not found: {args.input}")
        return [args.input]
    directory = args.input_dir
    if not directory.is_dir():
        raise SystemExit(f"Input directory not found: {directory}")
    files = sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in INPUT_EXTENSIONS
    )
    if not files:
        raise SystemExit(
            f"No .md/.txt/.json files found in {directory}"
        )
    log.info("Batch mode: %d file(s) queued from %s", len(files), directory)
    return files


def confirm_image_heavy(path: Path, stats, auto_yes: bool) -> bool:
    """Warn about an image-heavy PDF; return True to proceed."""
    print(
        f"\n⚠ WARNING: This PDF appears to be image-heavy: {path.name}\n"
        f"  Pages scanned:     {stats.total_pages}\n"
        f"  Pages with text:   {stats.text_pages} ({stats.text_pct}%)\n"
        f"  Pages image-only:  {stats.image_only_pages} "
        f"({stats.image_only_pct}%)\n"
        f"\n"
        f"  Image-heavy PDFs lose visual content during text extraction.\n"
        f"  Diagrams, photos, scanned handwriting, and drawings will NOT\n"
        f"  be captured in the extracted notes.\n"
    )
    if auto_yes:
        log.info("--yes: auto-confirming image-heavy PDF %s.", path.name)
        return True
    try:
        answer = input("  Proceed anyway? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def confirm_proceed_small(report, args) -> bool:
    """Warn that optimized content is tiny; return True to proceed anyway."""
    print(f"\n⚠ After optimization only {report.final_tokens} tokens remain "
          f"(from {report.raw_tokens}). Extraction may yield little.\n")
    if getattr(args, "yes", False):
        log.info("--yes: proceeding with tiny optimized content.")
        return True
    try:
        answer = input("  Proceed with extraction anyway? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def build_fallback_moc(notes: list, meta: extractor.SessionMeta,
                       detected_client: str | None = None) -> Note:
    """Deterministically rebuild the MOC from the final note set.

    Used when chunking produced multiple (partial) per-chunk MOCs, or when
    the LLM failed to emit one.
    """
    by_domain: dict = {}
    for note in notes:
        by_domain.setdefault(note.domain, []).append(note)

    lines = [f"# MOC - {meta.session_name}", ""]
    for domain in sorted(by_domain):
        pretty = domain.replace("-", " ").title()
        lines.append(f"## {pretty}")
        for note in by_domain[domain]:
            lines.append(f"- [[{note.title}]] ({note.note_type})")
        lines.append("")

    frontmatter = {
        "type": "moc",
        "source_domain": "general",
        "confidence": "high",
        "tags": ["moc", "index"],
        "related": [],
    }
    if detected_client:
        frontmatter["detected_client"] = detected_client
    return Note(
        title=f"MOC - {meta.session_name}",
        frontmatter=frontmatter,
        body="\n".join(lines).strip(),
    )


def inject_moc_warning(moc: Note, pct: int) -> None:
    """Prepend the image-only warning blockquote at the top of the MOC body."""
    quote = (
        f"> ⚠ This source PDF was {pct}% image-only. Notes below reflect\n"
        f"> only the extractable text. Visual content (diagrams, photos,\n"
        f"> scanned pages) was not captured."
    )
    lines = moc.body.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        rest = "\n".join(lines[1:]).lstrip("\n")
        moc.body = f"{lines[0]}\n\n{quote}\n\n{rest}"
    else:
        moc.body = f"{quote}\n\n{moc.body}"


def separate_moc(notes: list) -> tuple:
    """Split (atoms, mocs) — MOCs are type:moc or 'MOC -' titled notes."""
    atoms, mocs = [], []
    for note in notes:
        is_moc = (note.note_type == "moc"
                  or note.title.lower().startswith("moc"))
        (mocs if is_moc else atoms).append(note)
    return atoms, mocs


def process_conversation(conv: Conversation, source_path: Path, fmt: str,
                         args, config: dict, client,
                         archive_ctx: dict | None = None,
                         image_only_pct: int | None = None) -> dict:
    """Run the full pipeline for one conversation. Returns a summary dict.

    archive_ctx, when provided, is a mutable per-file dict ({root, taxonomy,
    plan}); the archive destination is planned from the first conversation's
    notes and the resulting filename is stamped into every note's
    frontmatter as archived_source. image_only_pct triggers the PDF
    extraction-warning frontmatter and MOC banner.
    """
    session_name = (
        args.session or conv.session_name or source_path.stem
    )
    meta = extractor.SessionMeta(
        session_name=session_name,
        source_file=source_path.name,
        source_format=fmt,
        created=date.today().isoformat(),
    )
    log.info("Processing session '%s' (%d turns)", session_name,
             len(conv.turns))

    # --- Pre-send token optimization (after parse, before extraction) ---
    turns = conv.turns
    opt_report = None
    if not getattr(args, "no_optimize", False):
        turns, opt_report = extractor.optimize_conversation(
            conv.turns, config, getattr(args, "optimize_mode", None))
        if (opt_report and opt_report.too_small_after
                and not args.dry_run
                and not confirm_proceed_small(opt_report, args)):
            log.warning("Session '%s' skipped — user declined tiny optimized "
                        "content.", session_name)
            return {
                "session": session_name, "notes": [], "dangling": [],
                "chunks": 0, "duplicates": 0, "moc": None,
                "cross_links": cross_linker.empty_stats(),
                "optimization": opt_report,
            }

    # --- Chunking decision ---
    plan = chunker.plan_chunks(
        turns,
        context_window=int(config.get("context_window_tokens", 200000)),
        threshold_ratio=float(config.get("chunk_threshold_ratio", 0.8)),
        overlap_turns=int(config.get("chunk_overlap_turns", 10)),
    )

    # --- Extraction ---
    note_sets = []
    for i, chunk_turns in enumerate(plan.chunks, 1):
        chunk_info = (
            f"chunk {i} of {plan.count}" if plan.count > 1 else None
        )
        notes = extractor.extract_notes(
            client, chunk_turns, meta, chunk_info=chunk_info,
        )
        note_sets.append(notes)

    # --- Merge & MOC handling ---
    duplicates = 0
    detected_client = None

    def capture_client(mocs: list) -> None:
        nonlocal detected_client
        for m in mocs:
            value = str(m.frontmatter.get("detected_client") or "").strip()
            if value and not detected_client:
                detected_client = value

    if plan.count > 1:
        # Per-chunk MOCs are partial; drop them and rebuild after the merge
        # (capturing detected_client before they go).
        stripped_sets = []
        for notes in note_sets:
            atoms, mocs = separate_moc(notes)
            capture_client(mocs)
            if mocs:
                log.info("Discarding %d partial per-chunk MOC(s) — the MOC "
                         "will be rebuilt over the merged set.", len(mocs))
            stripped_sets.append(atoms)
        atoms, duplicates = chunker.merge_chunk_results(
            stripped_sets,
            threshold=float(config.get("dedupe_similarity", 0.85)),
        )
        moc = (build_fallback_moc(atoms, meta, detected_client)
               if atoms else None)
    else:
        all_notes = note_sets[0]
        atoms, mocs = separate_moc(all_notes)
        capture_client(mocs)
        moc = mocs[-1] if mocs else (
            build_fallback_moc(atoms, meta, detected_client)
            if atoms else None
        )
        if not mocs and atoms:
            log.warning("LLM did not emit a MOC — built one programmatically.")

    final_notes = atoms + ([moc] if moc else [])
    if not final_notes:
        log.warning("Session '%s' produced no notes.", session_name)
        return {
            "session": session_name, "notes": [], "dangling": [],
            "chunks": plan.count, "duplicates": 0, "moc": None,
            "cross_links": cross_linker.empty_stats(),
            "optimization": opt_report,
        }

    # --- PDF extraction warning (confirmed image-heavy source) ---
    if image_only_pct is not None:
        warning_text = (f"Source PDF was {image_only_pct}% image-only — "
                        f"visual content not captured")
        for note in final_notes:
            note.frontmatter["extraction_warning"] = warning_text
        if moc:
            inject_moc_warning(moc, image_only_pct)

    # --- Archive planning & traceability stamping ---
    if archive_ctx is not None:
        if archive_ctx.get("plan") is None:
            domain_counts = Counter(n.domain for n in atoms)
            majority_domain = (domain_counts.most_common(1)[0][0]
                               if domain_counts else None)
            topic_title = moc.title if moc else session_name
            archive_ctx["plan"] = archiver.plan_archive(
                source_path, archive_ctx["root"], archive_ctx["taxonomy"],
                fmt, majority_domain, detected_client, topic_title,
                meta.created,
            )
        for note in final_notes:
            note.frontmatter["archived_source"] = archive_ctx["plan"].filename

    output_dir = resolve_output_dir(args, config)

    # --- Cross-session wikilink resolution (against the EXISTING vault) ---
    # Runs before the new notes are written, so the index naturally excludes
    # them. Injects forward links into the new notes (in memory) and writes
    # reciprocal backlinks into matched old notes on disk (skipped in dry-run).
    cross_stats = cross_linker.empty_stats()
    if not args.no_cross_link:
        cross_stats = cross_linker.link_against_vault(
            final_notes, output_dir, session_name, config,
            dry_run=args.dry_run,
        )

    # --- Write ---
    written = writer.write_notes(final_notes, output_dir, meta,
                                 dry_run=args.dry_run)

    # --- Post-processing: wikilink validation ---
    dangling = linker.validate_links(written)

    return {
        "session": session_name,
        "notes": written,
        "dangling": dangling,
        "chunks": plan.count,
        "duplicates": duplicates,
        "moc": moc,
        "cross_links": cross_stats,
        "optimization": opt_report,
    }


def _aggregate_cross_stats(results: list) -> dict:
    """Sum the per-session cross-link stats into one block for the summary."""
    agg = cross_linker.empty_stats()
    agg["enabled"] = any(r.get("cross_links", {}).get("enabled")
                         for r in results)
    for key in ("forward", "forward_notes", "backlinks", "backlink_notes",
                "failures", "backups"):
        agg[key] = sum(r.get("cross_links", {}).get(key, 0) for r in results)
    for r in results:
        cs = r.get("cross_links", {})
        agg["preview"].extend(cs.get("preview", []))
        if "backup_ext" in cs:
            agg["backup_ext"] = cs["backup_ext"]
    return agg


def print_summary(results: list, fmt_by_file: dict, client,
                  skipped: list | None = None,
                  archives: list | None = None,
                  dry_run: bool = False) -> None:
    notes = [w for r in results for w in r["notes"]]
    by_type = Counter(w.note.note_type for w in notes)
    by_domain = Counter(w.note.domain for w in notes)
    dangling = [d for r in results for d in r["dangling"]]
    total_chunks = sum(r["chunks"] for r in results)
    total_dupes = sum(r["duplicates"] for r in results)
    cross = _aggregate_cross_stats(results)

    print("\n" + "=" * 72)
    print("ATOMIZER SUMMARY")
    print("=" * 72)
    print(f"Sessions processed:   {len(results)}")
    print(f"Total notes:          {len(notes)}")
    print(f"  By type:            "
          + ", ".join(f"{t}: {c}" for t, c in sorted(by_type.items())))
    print(f"  By domain:          "
          + ", ".join(f"{d}: {c}" for d, c in sorted(by_domain.items())))
    for path, fmt in fmt_by_file.items():
        print(f"Input format:         {path.name} -> {fmt}")
    if skipped:
        print(f"Skipped (already processed): {len(skipped)} — "
              + ", ".join(p.name for p in skipped))
    print(f"Chunks used:          {total_chunks}")
    if total_dupes:
        print(f"Duplicates merged:    {total_dupes}")
    if dangling:
        print(f"Dangling wikilinks:   {len(dangling)}")
        for d in dangling:
            print(f"  - '{d.source_title}' -> [[{d.target}]]")
    else:
        print("Dangling wikilinks:   0")
    for line in cross_linker.format_stats_block(cross):
        print(line)

    # Optimization: per-file token savings (and batch totals when >1 file).
    opt_reports = [r.get("optimization") for r in results
                   if r.get("optimization")]
    active_opt = [o for o in opt_reports if not o.skipped]
    if len(active_opt) == 1:
        for line in optimizer.format_report(active_opt[0], per_pass=False):
            print(line)
    elif len(active_opt) > 1:
        for line in optimizer.format_batch_totals(opt_reports):
            print(line)

    for original, destination in (archives or []):
        print(f"📦 Archived: {original} → {destination}")

    usage = client.usage if client else None
    if usage and usage.calls:
        print(f"LLM calls:            {usage.calls}")
        print(f"Token usage:          {usage.input_tokens:,} input / "
              f"{usage.output_tokens:,} output")
        cost = usage.estimated_cost(client.model)
        if cost is not None:
            print(f"Estimated cost:       ${cost:.4f} ({client.model})")
        else:
            print(f"Estimated cost:       n/a (no pricing entry for "
                  f"'{client.model}' in config.yaml)")

    # Print each session's MOC as confirmation.
    for r in results:
        if r["moc"]:
            print("\n" + "-" * 72)
            print(f"MAP OF CONTENT — {r['session']}")
            print("-" * 72)
            print(r["moc"].body)

    # In --dry-run, show what cross-session links would be created.
    if dry_run and cross.get("preview"):
        print()
        for line in cross_linker.format_preview(cross["preview"]):
            print(line)
    print()


def run_optimize_only(args, config: dict, inputs: list, fmt_arg) -> int:
    """--optimize-only: parse + optimize, print report + cleaned text. No LLM
    extraction (the local optimizer LLM may still run for mode llm/both)."""
    reports = []
    for path in inputs:
        fmt, conversations = parse_file(path, fmt_arg)
        for conv in conversations:
            turns, report = extractor.optimize_conversation(
                conv.turns, config, getattr(args, "optimize_mode", None))
            reports.append(report)
            name = conv.session_name or path.stem
            print("\n" + "=" * 72)
            print(f"OPTIMIZE-ONLY — {path.name} :: {name}")
            print("=" * 72)
            for line in optimizer.format_report(report, per_pass=True):
                print(line)
            print("\n" + "-" * 72)
            print("CLEANED TEXT")
            print("-" * 72)
            for i, turn in enumerate(turns, 1):
                print(f"[{turn['role'].upper()} {i}]")
                print(turn["content"])
                print()
    if len([r for r in reports if not r.skipped]) > 1:
        print("\n" + "=" * 72)
        for line in optimizer.format_batch_totals(reports):
            print(line)
        print()
    return 0


def run_cross_link_only(args, config: dict) -> int:
    """--cross-link-only: re-link the whole vault in place (no LLM call)."""
    vault_dir = resolve_output_dir(args, config)
    log.info("Cross-link-only mode: re-linking vault at %s%s",
             vault_dir, " (dry-run)" if args.dry_run else "")
    stats = cross_linker.relink_vault(vault_dir, config, dry_run=args.dry_run)

    print("\n" + "=" * 72)
    print("ATOMIZER — CROSS-LINK-ONLY")
    print("=" * 72)
    for line in cross_linker.format_stats_block(stats):
        print(line)
    if args.dry_run and stats.get("preview"):
        print()
        for line in cross_linker.format_preview(stats["preview"]):
            print(line)
    print()
    return 0


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config = load_config(args.config)

    # --status: print the manifest summary and exit.
    if args.status:
        mf = manifest_mod.Manifest.load(resolve_output_dir(args, config))
        for line in mf.status_lines():
            print(line)
        return 0

    # --cross-link-only: re-link the whole vault in place, no LLM call.
    if args.cross_link_only:
        return run_cross_link_only(args, config)

    if not args.input and not args.input_dir:
        parser.error("one of --input or --input-dir is required "
                     "(or --status / --cross-link-only)")

    inputs = gather_inputs(args)
    fmt_arg = None if args.format == "auto" else args.format

    # --parse-only: print normalized conversations, never touch the LLM.
    if args.parse_only:
        for path in inputs:
            fmt, conversations = parse_file(path, fmt_arg)
            for conv in conversations:
                print(json.dumps(
                    {
                        "source_file": path.name,
                        "format": fmt,
                        "session_name": conv.session_name,
                        "turn_count": len(conv.turns),
                        "turns": conv.turns,
                    },
                    indent=2, ensure_ascii=False,
                ))
        return 0

    # --optimize-only: parse + optimize, print report; no extraction LLM.
    if args.optimize_only:
        return run_optimize_only(args, config, inputs, fmt_arg)

    # Deduplication: skip inputs whose content hash is already recorded.
    mf = manifest_mod.Manifest.load(resolve_output_dir(args, config))
    pending = []
    skipped = []
    for path in inputs:
        sha256 = manifest_mod.file_sha256(path)
        entry = mf.find_by_hash(sha256)
        if entry and not args.force:
            log.warning(
                "⚠ Skipping %s — already processed on %s (%s notes). "
                "Use --force to reprocess.",
                path.name, entry.get("processed_date", "unknown date"),
                entry.get("notes_generated", "?"),
            )
            skipped.append(path)
        else:
            if entry and args.force:
                log.info("--force: reprocessing %s despite manifest entry "
                         "from %s.", path.name,
                         entry.get("processed_date", "unknown date"))
            pending.append((path, sha256))

    if not pending:
        log.info("Nothing to do — all %d input file(s) already processed.",
                 len(skipped))
        print_summary([], {}, None, skipped=skipped)
        return 0

    try:
        client = llm_client.build_client(config, args.provider, args.model)
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc

    archive_root_cfg = config.get("archive_root")
    archive_active = (
        bool(config.get("archive_enabled", False))
        and bool(archive_root_cfg)
        and not args.no_archive
        and not args.dry_run
    )

    results = []
    fmt_by_file: dict = {}
    archives = []
    failures = 0
    for path, sha256 in pending:
        try:
            fmt, conversations = parse_file(path, fmt_arg)
            fmt_by_file[path] = fmt

            # Image-heavy PDF gate — before any LLM tokens are spent.
            image_only_pct = None
            if fmt == "pdf":
                stats = pdf_parser.analyze(path)
                if stats.is_image_heavy:
                    if not confirm_image_heavy(path, stats, args.yes):
                        log.warning(
                            "⏭ Skipped %s — user declined image-heavy PDF "
                            "processing.", path.name,
                        )
                        continue
                    image_only_pct = stats.image_only_pct

            archive_ctx = None
            if archive_active:
                archive_ctx = {
                    "root": Path(str(archive_root_cfg)).expanduser(),
                    "taxonomy": config.get("archive_taxonomy", {}) or {},
                    "plan": None,
                }

            tokens_in_before = client.usage.input_tokens
            tokens_out_before = client.usage.output_tokens

            file_results = []
            for conv in conversations:
                file_results.append(
                    process_conversation(
                        conv, path, fmt, args, config, client,
                        archive_ctx=archive_ctx,
                        image_only_pct=image_only_pct,
                    )
                )
            results.extend(file_results)

            file_tokens_in = client.usage.input_tokens - tokens_in_before
            file_tokens_out = client.usage.output_tokens - tokens_out_before
            total_notes = sum(len(r["notes"]) for r in file_results)

            # Archive only after a fully successful run that produced notes.
            archived_as = None
            if archive_ctx and archive_ctx.get("plan") and total_notes:
                try:
                    archiver.archive_file(path, archive_ctx["plan"])
                    archived_as = archive_ctx["plan"].display()
                    archives.append((path.name, archived_as))
                except OSError as exc:
                    log.error("Could not archive %s: %s — file left in "
                              "place.", path.name, exc)

            if not args.dry_run:
                sessions = [r["session"] for r in file_results]
                cost = client.usage.cost_for(
                    client.model, file_tokens_in, file_tokens_out)
                mf.record({
                    "input_file": path.name,
                    "original_path": str(path),
                    "sha256": sha256,
                    "processed_date": datetime.now().isoformat(
                        timespec="seconds"),
                    "session_name": ", ".join(dict.fromkeys(sessions))
                                    or path.stem,
                    "notes_generated": total_notes,
                    "output_files": [
                        w.path.name
                        for r in file_results for w in r["notes"] if w.path
                    ],
                    "archived_as": archived_as,
                    "token_usage": {"input": file_tokens_in,
                                    "output": file_tokens_out},
                    "estimated_cost": (round(cost, 4)
                                       if cost is not None else None),
                })
        except (ValueError, RuntimeError) as exc:
            failures += 1
            log.error("Failed to process %s: %s", path, exc)
            if not args.input_dir:
                raise SystemExit(f"Error: {exc}") from exc

    print_summary(results, fmt_by_file, client, skipped=skipped,
                  archives=archives, dry_run=args.dry_run)
    if failures:
        log.warning("%d file(s) failed — see errors above.", failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
