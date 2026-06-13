"""DOCX input parser (python-docx).

- Walks the document body in order, interleaving paragraphs and tables.
- Heading styles become markdown headings (Heading 1 -> #, etc.).
- Bold/italic runs become **bold** / *italic* markdown.
- Tables are converted to markdown tables.
- Images are stripped with a log note ("image skipped at paragraph N").
- Speaker turns are detected with the shared markdown engine; if none are
  found the full document becomes a single content block.
"""

import logging
import re

from parsers.markdown_parser import clean_content, extract_turns

log = logging.getLogger("atomizer.parsers.docx")

_HEADING_RE = re.compile(r"^heading\s+(\d)$", re.IGNORECASE)


def _require_docx():
    try:
        import docx
        return docx
    except ImportError as exc:
        raise RuntimeError(
            "The 'python-docx' package is required for DOCX input. "
            "Install with: pip install python-docx"
        ) from exc


def _run_to_markdown(run) -> str:
    text = run.text
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return text
    bold = bool(run.bold)
    italic = bool(run.italic)
    if bold and italic:
        marked = f"***{stripped}***"
    elif bold:
        marked = f"**{stripped}**"
    elif italic:
        marked = f"*{stripped}*"
    else:
        return text
    # Re-attach surrounding whitespace outside the markers.
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    return f"{leading}{marked}{trailing}"


def _paragraph_has_image(paragraph) -> bool:
    try:
        return bool(paragraph._p.xpath(".//w:drawing | .//w:pict"))
    except Exception:
        return False


def _paragraph_to_markdown(paragraph, index: int) -> str:
    if _paragraph_has_image(paragraph):
        log.info("image skipped at paragraph %d", index)

    text = "".join(_run_to_markdown(run) for run in paragraph.runs)
    if not text.strip():
        return ""

    style_name = ""
    try:
        style_name = paragraph.style.name or ""
    except Exception:
        pass

    m = _HEADING_RE.match(style_name.strip())
    if m:
        level = min(int(m.group(1)), 6)
        return f"{'#' * level} {text.strip()}"
    if style_name.strip().lower() == "title":
        return f"# {text.strip()}"
    if style_name.strip().lower().startswith("list"):
        return f"- {text.strip()}"
    return text


def _table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = [
            re.sub(r"\s+", " ", cell.text).strip().replace("|", "\\|")
            for cell in row.cells
        ]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _iter_block_items(document):
    """Yield Paragraph and Table objects in true document order."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def parse_path(path) -> list:
    """Parse a DOCX file. Returns [(session_name, turns)]."""
    docx = _require_docx()
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ValueError(f"Could not open DOCX file {path}: {exc}") from exc

    blocks = []
    paragraph_index = 0
    for item in _iter_block_items(document):
        if isinstance(item, Paragraph):
            paragraph_index += 1
            md = _paragraph_to_markdown(item, paragraph_index)
            if md:
                blocks.append(md)
        else:  # Table
            md = _table_to_markdown(item)
            if md:
                blocks.append(md)

    full_text = "\n\n".join(blocks)
    turns = extract_turns(full_text)
    if len(turns) < 2:
        log.info("DOCX parser: no speaker turns detected — treating document "
                 "as a single content block.")
        content = clean_content(full_text)
        turns = (
            [{"role": "human", "content": content, "timestamp": None}]
            if content else []
        )
    else:
        log.info("DOCX parser detected %d speaker turn(s).", len(turns))

    log.info("DOCX parser extracted %d block(s), %d turn(s)",
             len(blocks), len(turns))
    return [(None, turns)]
