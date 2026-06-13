"""Post-processing auto-archive with structured global identification naming.

After successful atomization, the input file is moved into the archive tree:

    {archive_root}/{YYYY-MM}/{Date}_{Seq}_{Domain}-{Client}-{ContentType}_{Topic}.{ext}

e.g.  archive/2026-06/2026-06-11_001_TST-MEX-EMAIL_BCC-IK45E-U314.txt

Codes come from the user-editable `archive_taxonomy` table in config.yaml:
- Domain: 3-letter code mapped from the majority source_domain of the notes
- Client: 3-letter code looked up from the LLM's `detected_client` value
- ContentType: mapped from the input format/parser used
- Topic: derived from the MOC title, max 50 chars, alphanumeric + hyphens

Unresolvable domain/client/content-type codes fall back to UNK. The
sequence number is derived from what already exists in the target month
folder (not the manifest), so manually added files are respected.
"""

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("atomizer.archiver")

UNKNOWN_CODE = "UNK"
TOPIC_MAX_LEN = 50

_SEQ_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{3})_")
_MOC_PREFIX_RE = re.compile(r"^\s*moc\s*[-–—:]\s*", re.IGNORECASE)


@dataclass
class ArchivePlan:
    target: Path        # full destination path
    archive_root: Path  # for building display strings

    @property
    def filename(self) -> str:
        return self.target.name

    def display(self) -> str:
        """Path string for logs/manifest, e.g. 'Archive/2026-06/file.txt'."""
        try:
            return self.target.relative_to(self.archive_root.parent).as_posix()
        except ValueError:
            return self.target.as_posix()


def lookup_code(table: dict, value: str | None,
                default: str = UNKNOWN_CODE) -> str:
    """Resolve a taxonomy code: exact match first, then substring match."""
    if not value or not isinstance(table, dict):
        return default
    needle = str(value).strip().lower()
    if not needle:
        return default
    lowered = {str(k).strip().lower(): str(v) for k, v in table.items()}
    if needle in lowered:
        return lowered[needle]
    # Substring fallback: "interlake mecalux inc." should still hit "mecalux".
    best = None
    for key, code in lowered.items():
        if key and (key in needle or needle in key):
            if best is None or len(key) > best[0]:
                best = (len(key), code)
    return best[1] if best else default


def sanitize_topic(title: str | None, max_length: int = TOPIC_MAX_LEN) -> str:
    """MOC title -> archive topic: alphanumeric and hyphens only, <=50 chars."""
    if not title:
        return "Untitled"
    topic = _MOC_PREFIX_RE.sub("", title)
    topic = unicodedata.normalize("NFKD", topic)
    topic = topic.encode("ascii", "ignore").decode("ascii")
    topic = re.sub(r"[^A-Za-z0-9]+", "-", topic).strip("-")
    topic = re.sub(r"-{2,}", "-", topic)
    return topic[:max_length].rstrip("-") or "Untitled"


def next_sequence(month_dir: Path, date_str: str) -> int:
    """Next same-day sequence, based on files already in the month folder."""
    highest = 0
    if month_dir.is_dir():
        for entry in month_dir.iterdir():
            m = _SEQ_RE.match(entry.name)
            if m and m.group(1) == date_str:
                highest = max(highest, int(m.group(2)))
    return highest + 1


def plan_archive(input_path: Path, archive_root: Path, taxonomy: dict,
                 fmt: str, domain: str | None, detected_client: str | None,
                 topic_title: str | None, date_str: str) -> ArchivePlan:
    """Compute the archive destination for an input file (no file moved yet)."""
    taxonomy = taxonomy or {}
    domain_code = lookup_code(taxonomy.get("domains", {}), domain)
    client_code = lookup_code(taxonomy.get("clients", {}), detected_client)
    content_code = lookup_code(taxonomy.get("content_types", {}), fmt)
    topic = sanitize_topic(topic_title)
    ext = input_path.suffix.lower()

    month_dir = archive_root / date_str[:7]
    seq = next_sequence(month_dir, date_str)
    name = (f"{date_str}_{seq:03d}_"
            f"{domain_code}-{client_code}-{content_code}_{topic}{ext}")
    plan = ArchivePlan(target=month_dir / name, archive_root=archive_root)
    log.info(
        "Archive plan for %s: %s (domain=%s->%s, client=%r->%s, "
        "content=%s->%s)",
        input_path.name, plan.display(), domain, domain_code,
        detected_client, client_code, fmt, content_code,
    )
    return plan


def _bump_sequence(target: Path) -> Path:
    """Resolve a same-name collision by incrementing the sequence number."""
    m = _SEQ_RE.match(target.name)
    if not m:
        return target.with_name(f"{target.stem}-2{target.suffix}")
    seq = int(m.group(2)) + 1
    rest = target.name[m.end():]
    return target.with_name(f"{m.group(1)}_{seq:03d}_{rest}")


def archive_file(input_path: Path, plan: ArchivePlan) -> Path:
    """Move the input file to its planned archive location."""
    target = plan.target
    target.parent.mkdir(parents=True, exist_ok=True)
    # Another file may have claimed the sequence between plan and move.
    while target.exists():
        target = _bump_sequence(target)
    shutil.move(str(input_path), str(target))
    plan.target = target
    log.info("Archived %s -> %s", input_path.name, plan.display())
    return target
