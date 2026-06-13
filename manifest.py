"""Input-file deduplication manifest.

Tracks every input file ever processed in a human-readable JSON manifest at
{output_dir}/.atomizer-manifest.json. Files are identified by the SHA-256
of their content, so a renamed copy is still recognized and an edited file
is treated as new. Entries are updated in place when the same content is
reprocessed with --force.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("atomizer.manifest")

MANIFEST_FILENAME = ".atomizer-manifest.json"


def file_sha256(path: Path) -> str:
    """SHA-256 hex digest of a file's raw bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Manifest:
    path: Path
    processed: list

    @classmethod
    def load(cls, output_dir: Path) -> "Manifest":
        path = output_dir / MANIFEST_FILENAME
        processed: list = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                entries = data.get("processed", []) if isinstance(data, dict) else []
                processed = [e for e in entries if isinstance(e, dict)]
                log.debug("Loaded manifest with %d entr(ies) from %s",
                          len(processed), path)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning(
                    "Manifest %s is unreadable (%s) — starting a fresh "
                    "manifest; the old file will be overwritten on the next "
                    "successful run.", path, exc,
                )
        return cls(path=path, processed=processed)

    def find_by_hash(self, sha256: str) -> dict | None:
        """Return the manifest entry for an exact content hash, if any."""
        for entry in self.processed:
            if entry.get("sha256") == sha256:
                return entry
        return None

    def record(self, entry: dict) -> None:
        """Insert or update (by sha256) an entry, then persist."""
        existing = self.find_by_hash(entry.get("sha256", ""))
        if existing is not None:
            existing.update(entry)
            log.info("Manifest: updated entry for %s", entry.get("input_file"))
        else:
            self.processed.append(entry)
            log.info("Manifest: recorded %s (%d note(s))",
                     entry.get("input_file"), entry.get("notes_generated", 0))
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"processed": self.processed}, indent=2,
                       ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n",
        )
        log.debug("Manifest saved to %s", self.path)

    def status_lines(self) -> list:
        """Human-readable summary lines for --status."""
        if not self.processed:
            return [f"No files processed yet (manifest: {self.path})."]
        total_notes = sum(int(e.get("notes_generated", 0))
                          for e in self.processed)
        latest = max(self.processed,
                     key=lambda e: str(e.get("processed_date", "")))
        latest_date = str(latest.get("processed_date", ""))[:10]
        return [
            f"Processed: {len(self.processed)} files | {total_notes} notes "
            f"| Last: {latest_date}"
        ]
