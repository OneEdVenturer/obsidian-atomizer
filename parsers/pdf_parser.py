"""PDF input parser (pdfplumber).

- Extracts text page by page; tables are rendered as markdown tables and
  their regions are excluded from the plain-text pass so content isn't
  duplicated.
- Repeated first/last lines across pages (headers/footers) are stripped.
- Simple two-column layouts (common in test reports) are detected via a
  clear vertical gutter and read left column first, then right.
- Speaker turns are detected with the shared markdown engine (email chains
  saved as PDF); if none are found the whole document becomes one block.

Image-heaviness analysis (`analyze`) is exposed separately so the CLI can
warn the user *before* any LLM tokens are spent.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass

from parsers.markdown_parser import clean_content, extract_turns

log = logging.getLogger("atomizer.parsers.pdf")

# A page with less than this much extractable text AND at least one image
# counts as "image-only".
_IMAGE_ONLY_TEXT_THRESHOLD = 50
# Fraction of image-only pages above which the PDF is flagged image-heavy.
_IMAGE_HEAVY_RATIO = 0.5
# Header/footer lines must repeat on more than this fraction of pages.
_REPEAT_RATIO = 0.5


def _require_pdfplumber():
    try:
        import pdfplumber
        return pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "The 'pdfplumber' package is required for PDF input. "
            "Install with: pip install pdfplumber"
        ) from exc


@dataclass
class PdfStats:
    total_pages: int
    image_only_pages: int

    @property
    def text_pages(self) -> int:
        return self.total_pages - self.image_only_pages

    @property
    def image_only_pct(self) -> int:
        if not self.total_pages:
            return 0
        return round(100 * self.image_only_pages / self.total_pages)

    @property
    def text_pct(self) -> int:
        if not self.total_pages:
            return 0
        return round(100 * self.text_pages / self.total_pages)

    @property
    def is_image_heavy(self) -> bool:
        return (self.total_pages > 0
                and self.image_only_pages / self.total_pages
                > _IMAGE_HEAVY_RATIO)


def analyze(path) -> PdfStats:
    """Count image-only pages (text < 50 chars AND images present)."""
    pdfplumber = _require_pdfplumber()
    total = 0
    image_only = 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            total += 1
            text = (page.extract_text() or "").strip()
            if len(text) < _IMAGE_ONLY_TEXT_THRESHOLD and page.images:
                image_only += 1
    stats = PdfStats(total_pages=total, image_only_pages=image_only)
    log.info(
        "PDF analysis for %s: %d page(s), %d image-only (%d%%)",
        getattr(path, "name", path), stats.total_pages,
        stats.image_only_pages, stats.image_only_pct,
    )
    return stats


def _obj_in_bbox(obj: dict, bbox) -> bool:
    """True if the object's center falls inside the bbox (x0, top, x1, bottom)."""
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def _table_to_markdown(rows: list) -> str:
    """Render pdfplumber table rows (lists of cells) as a markdown table."""
    cleaned = []
    for row in rows:
        cells = [
            re.sub(r"\s+", " ", (cell or "")).strip().replace("|", "\\|")
            for cell in row
        ]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    lines = ["| " + " | ".join(cleaned[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _is_two_column(page) -> bool:
    """Detect a clear vertical gutter splitting the words into two halves."""
    try:
        words = page.extract_words()
    except Exception:
        return False
    if len(words) < 20:
        return False
    mid = page.width / 2
    band = page.width * 0.04
    left = right = crossing = 0
    for w in words:
        if w["x1"] < mid - band:
            left += 1
        elif w["x0"] > mid + band:
            right += 1
        else:
            crossing += 1
    return (left >= 10 and right >= 10
            and crossing <= max(1, len(words) * 0.02))


def _extract_page_text(page) -> tuple:
    """Return (text_lines, table_markdown_blocks) for one page."""
    try:
        tables = page.find_tables()
    except Exception as exc:
        log.debug("Table detection failed on page %s: %s", page.page_number, exc)
        tables = []
    table_bboxes = [t.bbox for t in tables]
    table_md = []
    for t in tables:
        try:
            md = _table_to_markdown(t.extract())
        except Exception as exc:
            log.debug("Table extraction failed on page %s: %s",
                      page.page_number, exc)
            md = ""
        if md:
            table_md.append(md)

    if not table_bboxes and _is_two_column(page):
        mid = page.width / 2
        left = page.crop((0, 0, mid, page.height)).extract_text() or ""
        right = page.crop((mid, 0, page.width, page.height)).extract_text() or ""
        log.debug("Page %s: two-column layout detected.", page.page_number)
        text = (left.strip() + "\n" + right.strip()).strip()
    elif table_bboxes:
        filtered = page.filter(
            lambda obj: not any(_obj_in_bbox(obj, bb) for bb in table_bboxes)
        )
        text = filtered.extract_text() or ""
    else:
        text = page.extract_text() or ""

    return [ln.rstrip() for ln in text.splitlines()], table_md


def _normalize_repeat_key(line: str) -> str:
    """Normalize a header/footer candidate: drop digits (page numbers)."""
    line = unicodedata.normalize("NFKC", line)
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", line)).strip().lower()


def _strip_repeated_lines(page_lines: list) -> list:
    """Remove first/last lines repeated on more than half of the pages."""
    pages_with_text = [lines for lines in page_lines
                       if any(ln.strip() for ln in lines)]
    if len(pages_with_text) < 3:
        return page_lines

    def first_nonempty(lines):
        return next((ln for ln in lines if ln.strip()), None)

    def last_nonempty(lines):
        return next((ln for ln in reversed(lines) if ln.strip()), None)

    threshold = len(pages_with_text) * _REPEAT_RATIO
    for picker, label in ((first_nonempty, "header"),
                          (last_nonempty, "footer")):
        counts: dict = {}
        for lines in pages_with_text:
            candidate = picker(lines)
            if candidate:
                key = _normalize_repeat_key(candidate)
                counts[key] = counts.get(key, 0) + 1
        repeated = {k for k, n in counts.items() if n > threshold}
        if not repeated:
            continue
        for lines in page_lines:
            candidate = picker(lines)
            if candidate and _normalize_repeat_key(candidate) in repeated:
                lines.remove(candidate)
        log.info("Stripped repeated %s line(s) appearing across pages.", label)
    return page_lines


def parse_path(path) -> list:
    """Parse a PDF file. Returns [(session_name, turns)]."""
    pdfplumber = _require_pdfplumber()
    page_lines: list = []
    page_tables: list = []

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            lines, tables = _extract_page_text(page)
            page_lines.append(lines)
            page_tables.append(tables)

    page_lines = _strip_repeated_lines(page_lines)

    page_blocks = []
    for lines, tables in zip(page_lines, page_tables):
        parts = []
        text = "\n".join(lines).strip()
        if text:
            parts.append(text)
        parts.extend(tables)
        if parts:
            page_blocks.append("\n\n".join(parts))

    full_text = "\n\n".join(page_blocks)
    turns = extract_turns(full_text)
    if len(turns) < 2:
        log.info("PDF parser: no speaker turns detected — treating document "
                 "as a single content block.")
        content = clean_content(full_text)
        turns = (
            [{"role": "human", "content": content, "timestamp": None}]
            if content else []
        )
    else:
        log.info("PDF parser detected %d speaker turn(s).", len(turns))

    log.info("PDF parser extracted %d page(s), %d turn(s)",
             len(page_lines), len(turns))
    return [(None, turns)]
