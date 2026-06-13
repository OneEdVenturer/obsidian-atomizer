"""Pre-send token optimization pipeline.

Takes normalized conversation turns and returns a cleaned copy plus a token
reduction report, BEFORE the (paid) extraction LLM ever sees the text.

Three modes:
- rules : six deterministic mechanical cleaning passes. Instant, free.
- llm   : rules, then a LOCAL LLM removes remaining low-signal prose.
- both  : the same two-stage pipeline (rules -> local LLM). Canonical name.

The optimizer's LLM stage uses a SEPARATE, local-only provider — it NEVER
calls the paid Anthropic API. If the local server is unavailable or errors,
the LLM stage falls back to the rules-only output.

Token counts are exact (tiktoken cl100k_base, no safety multiplier) so the
reported savings reflect the real input the extraction model would receive.
"""

import difflib
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("atomizer.optimizer")

# Skip optimization for trivially small inputs; warn if a result is tiny.
MIN_OPTIMIZE_TOKENS = 500
TOO_SMALL_TOKENS = 100
# claude-sonnet-4-6 input price, USD per 1M tokens, for savings estimates.
SONNET_INPUT_RATE = 3.00

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def count_tokens(text: str) -> int:
    """Exact token count (tiktoken cl100k_base) or a ~4-chars/token fallback."""
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)


def count_turns(turns: list) -> int:
    return sum(count_tokens(t.get("content", "")) for t in turns)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Settings + report
# ---------------------------------------------------------------------------

@dataclass
class OptSettings:
    enabled: bool = True
    mode: str = "both"
    llm_provider: str = "local"
    llm_model: str = "auto"
    llm_endpoint: str = "http://localhost:1234/v1"
    llm_timeout: int = 600
    llm_max_tokens: int = 8192
    boilerplate_threshold: int = 3
    boilerplate_similarity: float = 0.90
    quoted_reply_similarity: float = 0.85
    duplicate_turn_similarity: float = 0.90
    min_token_saving_to_report: int = 50

    @classmethod
    def from_config(cls, config: dict) -> "OptSettings":
        o = (config or {}).get("optimization", {}) or {}
        return cls(
            enabled=bool(o.get("enabled", True)),
            mode=str(o.get("mode", "both")).lower(),
            llm_provider=str(o.get("llm_provider", "local")).lower(),
            llm_model=str(o.get("llm_model", "auto")),
            llm_endpoint=str(o.get("llm_endpoint", "http://localhost:1234/v1")),
            llm_timeout=int(o.get("llm_timeout", 600)),
            llm_max_tokens=int(o.get("llm_max_tokens", 8192)),
            boilerplate_threshold=int(o.get("boilerplate_threshold", 3)),
            boilerplate_similarity=float(o.get("boilerplate_similarity", 0.90)),
            quoted_reply_similarity=float(o.get("quoted_reply_similarity",
                                               0.85)),
            duplicate_turn_similarity=float(
                o.get("duplicate_turn_similarity", 0.90)),
            min_token_saving_to_report=int(
                o.get("min_token_saving_to_report", 50)),
        )

    def resolved_model(self) -> str:
        return "local-model" if self.llm_model in ("", "auto") else self.llm_model


@dataclass
class OptReport:
    mode: str
    enabled: bool = True
    skipped: bool = False
    skipped_reason: str = ""
    raw_tokens: int = 0
    after_rules_tokens: int = 0
    after_llm_tokens: int | None = None
    final_tokens: int = 0
    pass_breakdown: list = field(default_factory=list)  # (name, tokens_after)
    llm_used: bool = False
    llm_fallback: bool = False
    llm_fallback_reason: str = ""
    turns_dropped: int = 0
    too_small_after: bool = False
    messages: list = field(default_factory=list)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.raw_tokens - self.final_tokens)

    @property
    def reduction_pct(self) -> float:
        if not self.raw_tokens:
            return 0.0
        return 100.0 * self.tokens_saved / self.raw_tokens

    def cost_saving(self) -> float:
        return self.tokens_saved / 1_000_000 * SONNET_INPUT_RATE

    def worth_reporting(self, settings: OptSettings) -> bool:
        return self.tokens_saved >= settings.min_token_saving_to_report


# ---------------------------------------------------------------------------
# Content segmentation (protect code fences, YAML frontmatter)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"\s*(```|~~~)")


def _segment(content: str) -> list:
    """Split content into ('text'|'code'|'yaml', text) segments.

    Code fences and a leading YAML frontmatter block are returned as opaque
    'code'/'yaml' segments that the cleaning passes never touch.
    """
    lines = content.split("\n")
    segs = []
    i = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                segs.append(("yaml", "\n".join(lines[:j + 1])))
                i = j + 1
                break
    buf = []
    while i < len(lines):
        if _FENCE_RE.match(lines[i]) and lines[i].strip().startswith(("```", "~~~")):
            if buf:
                segs.append(("text", "\n".join(buf)))
                buf = []
            block = [lines[i]]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                closed = lines[i].strip().startswith(("```", "~~~"))
                i += 1
                if closed:
                    break
            segs.append(("code", "\n".join(block)))
        else:
            buf.append(lines[i])
            i += 1
    if buf:
        segs.append(("text", "\n".join(buf)))
    return segs


def _desegment(segs: list) -> str:
    return "\n".join(text for _, text in segs)


def _prose_like(paragraph: str) -> bool:
    """True if a paragraph is prose (not a data table / code / number block)."""
    lines = [ln for ln in paragraph.split("\n") if ln.strip()]
    if sum(1 for ln in lines if "|" in ln) >= 2:
        return False
    digits = sum(c.isdigit() for c in paragraph)
    if paragraph and digits / len(paragraph) > 0.3:
        return False
    return True


# ---------------------------------------------------------------------------
# Pass 1: Repeated boilerplate removal
# ---------------------------------------------------------------------------

def pass_boilerplate(turns: list, s: OptSettings, report: OptReport) -> list:
    seg_cache = []
    entries = []  # [turn_i, seg_i, para_i, text, norm]
    for ti, t in enumerate(turns):
        segs = _segment(t["content"])
        per = []
        for si, (kind, txt) in enumerate(segs):
            if kind == "text":
                paras = re.split(r"\n\s*\n", txt)
                per.append(("text", paras))
                for pi, para in enumerate(paras):
                    norm = _normalize(para)
                    if len(norm) >= 30 and _prose_like(para):
                        entries.append([ti, si, pi, para, norm])
            else:
                per.append((kind, txt))
        seg_cache.append(per)

    # Greedy clustering by >= boilerplate_similarity (length-gated for speed).
    reps = []  # {norm, members:[entry]}
    for e in entries:
        placed = False
        for rep in reps:
            if (abs(len(e[4]) - len(rep["norm"]))
                    <= 0.1 * max(len(e[4]), len(rep["norm"]))
                    and _similarity(e[4], rep["norm"]) >= s.boilerplate_similarity):
                rep["members"].append(e)
                placed = True
                break
        if not placed:
            reps.append({"norm": e[4], "members": [e]})

    remove = set()
    for rep in reps:
        if len(rep["members"]) >= s.boilerplate_threshold:
            first = rep["members"][0]
            for m in rep["members"][1:]:
                remove.add((m[0], m[1], m[2]))
            msg = (f"Boilerplate removed: '{first[3][:50]}...' appeared "
                   f"{len(rep['members'])} times")
            log.info(msg)
            report.messages.append(msg)

    if not remove:
        return turns

    out = []
    for ti, t in enumerate(turns):
        out_segs = []
        for si, item in enumerate(seg_cache[ti]):
            if item[0] == "text":
                kept = [p for pi, p in enumerate(item[1])
                        if (ti, si, pi) not in remove]
                out_segs.append(("text", "\n\n".join(kept)))
            else:
                out_segs.append((item[0], item[1]))
        out.append({**t, "content": _desegment(out_segs).strip()})
    return out


# ---------------------------------------------------------------------------
# Pass 2: Whitespace and formatting collapse
# ---------------------------------------------------------------------------

_SEPARATOR_RE = re.compile(r"[-=_]{11,}")


def pass_whitespace(turns: list, s: OptSettings, report: OptReport) -> list:
    out = []
    for t in turns:
        res = []
        for kind, txt in _segment(t["content"]):
            if kind in ("code", "yaml"):
                res.append((kind, txt))
                continue
            new_lines = []
            for line in txt.split("\n"):
                stripped = line.rstrip()
                if _SEPARATOR_RE.fullmatch(stripped.strip()):
                    continue  # decorative separator line
                if "|" not in stripped:  # protect table alignment
                    stripped = re.sub(r" {3,}", " ", stripped)
                new_lines.append(stripped)
            collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(new_lines))
            res.append((kind, collapsed))
        out.append({**t, "content": _desegment(res).strip()})
    return out


# ---------------------------------------------------------------------------
# Pass 3: Low-signal content removal
# ---------------------------------------------------------------------------

_EMAIL_HEADER_RE = re.compile(r"^(from|sent|to|cc|bcc|subject|date)\s*:", re.I)
_FORWARDED_RE = re.compile(r"\s*-+\s*forwarded message\s*-+\s*", re.I)
_STAMP_RE = re.compile(r"\s*(confidential|proprietary|do not distribute)"
                       r"[\s:!.\-]*", re.I)
_AUTO_FOOTER_RE = re.compile(r"\s*(sent from my \w+|get outlook for \w+)\s*",
                             re.I)
_DISCLAIMER_RE = re.compile(r"\s*this email and any attachments", re.I)
_PAGE_RE = re.compile(r"(?i)\bpage\s+\d+\s+of\s+\d+\b")
_PLACEHOLDER_RE = re.compile(r"(?i)\[(image|logo|signature|cid:[^\]]*)\]")


def _clean_lowsignal_text(txt: str, seen_headers: set) -> str:
    lines = txt.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Email header blocks: keep first unique sender block, drop repeats.
        if _EMAIL_HEADER_RE.match(line.strip()):
            block = []
            j = i
            while j < len(lines) and _EMAIL_HEADER_RE.match(lines[j].strip()):
                block.append(lines[j])
                j += 1
            norm = _normalize(" ".join(block))
            if norm in seen_headers:
                i = j
                continue
            seen_headers.add(norm)
            out.extend(block)
            i = j
            continue
        if _FORWARDED_RE.fullmatch(line) or _STAMP_RE.fullmatch(line) \
                or _AUTO_FOOTER_RE.fullmatch(line):
            i += 1
            continue
        if _DISCLAIMER_RE.match(line):
            while i < len(lines) and lines[i].strip():
                i += 1  # drop the disclaimer paragraph
            continue
        line = _PAGE_RE.sub("", line)
        line = _PLACEHOLDER_RE.sub("", line)
        out.append(line)
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def pass_lowsignal(turns: list, s: OptSettings, report: OptReport) -> list:
    seen_headers = set()
    out = []
    for t in turns:
        res = []
        for kind, txt in _segment(t["content"]):
            if kind in ("code", "yaml"):
                res.append((kind, txt))
                continue
            res.append((kind, _clean_lowsignal_text(txt, seen_headers)))
        out.append({**t, "content": _desegment(res).strip()})
    return out


# ---------------------------------------------------------------------------
# Pass 4: Table-of-contents / index stripping
# ---------------------------------------------------------------------------

_TOC_LINE_RE = re.compile(r"^.+?(\.{3,}|\t+|\s{3,})\s*\d{1,4}\s*$")


def pass_toc(turns: list, s: OptSettings, report: OptReport) -> list:
    out = []
    for t in turns:
        res = []
        for kind, txt in _segment(t["content"]):
            if kind in ("code", "yaml"):
                res.append((kind, txt))
                continue
            lines = txt.split("\n")
            keep = []
            i = 0
            while i < len(lines):
                j = i
                while j < len(lines) and lines[j].strip() \
                        and _TOC_LINE_RE.match(lines[j].strip()):
                    j += 1
                if j - i >= 5:
                    msg = f"TOC block removed: {j - i} lines"
                    log.info(msg)
                    report.messages.append(msg)
                    i = j
                    continue
                keep.append(lines[i])
                i += 1
            res.append((kind, "\n".join(keep)))
        out.append({**t, "content": _desegment(res).strip()})
    return out


# ---------------------------------------------------------------------------
# Pass 5: Redundant quoted-reply collapsing
# ---------------------------------------------------------------------------

_ATTRIBUTION_RE = re.compile(r"\s*On .+ wrote:\s*$")
PLACEHOLDER = "[previous message quoted -- already captured above]"


def _collapse_quotes(txt: str, corpus: str, s: OptSettings) -> str:
    lines = txt.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_attr = bool(_ATTRIBUTION_RE.match(line))
        if line.lstrip().startswith(">") or is_attr:
            block = []
            j = i
            if is_attr:
                block.append(lines[j])
                j += 1
            while j < len(lines) and (lines[j].lstrip().startswith(">")
                                      or not lines[j].strip()):
                block.append(lines[j])
                j += 1
            dequoted = [
                _normalize(re.sub(r"^\s*>+\s?", "", b))
                for b in block
                if b.strip() and not _ATTRIBUTION_RE.match(b)
            ]
            dequoted = [d for d in dequoted if d]
            if dequoted:
                hits = sum(1 for d in dequoted if d in corpus)
                if hits / len(dequoted) >= s.quoted_reply_similarity:
                    out.append(PLACEHOLDER)
                    i = j
                    continue
            out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def pass_quoted(turns: list, s: OptSettings, report: OptReport) -> list:
    seen = []
    out = []
    collapsed = 0
    for t in turns:
        res = []
        for kind, txt in _segment(t["content"]):
            if kind in ("code", "yaml"):
                res.append((kind, txt))
                seen.append(_normalize(txt))
                continue
            corpus = " ".join(seen)
            new_txt = _collapse_quotes(txt, corpus, s)
            if PLACEHOLDER in new_txt and PLACEHOLDER not in txt:
                collapsed += new_txt.count(PLACEHOLDER)
            res.append((kind, new_txt))
            seen.append(_normalize(new_txt))
        out.append({**t, "content": _desegment(res).strip()})
    if collapsed:
        msg = f"Quoted replies collapsed: {collapsed} block(s)"
        log.info(msg)
        report.messages.append(msg)
    return out


# ---------------------------------------------------------------------------
# Pass 6: Duplicate consecutive turn detection
# ---------------------------------------------------------------------------

def pass_duplicate_turns(turns: list, s: OptSettings,
                         report: OptReport) -> list:
    if len(turns) < 2:
        return turns
    keep = [True] * len(turns)
    for i in range(len(turns) - 1):
        if not keep[i]:
            continue
        a = _normalize(turns[i]["content"])
        b = _normalize(turns[i + 1]["content"])
        if a and b:
            ratio = _similarity(a, b)
            if ratio >= s.duplicate_turn_similarity:
                keep[i] = False  # keep the later (regenerated) turn
                msg = (f"Duplicate turn removed (turn {i}, "
                       f"{round(ratio * 100)}% similar to turn {i + 1})")
                log.info(msg)
                report.messages.append(msg)
    return [t for i, t in enumerate(turns) if keep[i]]


RULES_PASSES = [
    ("Boilerplate removal", pass_boilerplate),
    ("Whitespace collapse", pass_whitespace),
    ("Low-signal removal", pass_lowsignal),
    ("TOC stripping", pass_toc),
    ("Quoted-reply collapse", pass_quoted),
    ("Duplicate turns", pass_duplicate_turns),
]


def _prune_empty(turns: list, report: OptReport) -> tuple:
    out = []
    dropped = 0
    for t in turns:
        if t.get("content", "").strip():
            out.append(t)
        else:
            dropped += 1
    if dropped:
        report.turns_dropped += dropped
        report.messages.append(
            f"{dropped} turn(s) dropped (emptied by optimization)")
        log.warning("%d turn(s) dropped (emptied by optimization)", dropped)
    return out, dropped


# ---------------------------------------------------------------------------
# LLM intelligent cleaning (local only)
# ---------------------------------------------------------------------------

def _llm_clean(turns: list, s: OptSettings, llm_clean_fn,
               report: OptReport) -> list | None:
    """Per-turn local-LLM clean. Returns None to signal a rules-only fallback."""
    if llm_clean_fn is None:
        report.llm_fallback = True
        report.llm_fallback_reason = "no local cleaner configured"
        log.warning("Local LLM unavailable -- falling back to rules-only")
        return None

    cleaned = []
    for t in turns:
        try:
            out = llm_clean_fn(t["content"])
        except Exception as exc:  # connection refused, timeout, bad response
            report.llm_fallback = True
            report.llm_fallback_reason = f"local LLM error: {exc}"
            log.warning("Local LLM unavailable -- falling back to rules-only "
                        "(%s)", exc)
            return None
        if out is None or not out.strip():
            report.llm_fallback = True
            report.llm_fallback_reason = "local LLM returned empty output"
            log.warning("Local LLM returned empty output -- falling back to "
                        "rules-only")
            return None
        cleaned.append({**t, "content": out.strip()})
    return cleaned


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def optimize(turns: list, *, mode: str = "both",
             settings: OptSettings | None = None,
             llm_clean_fn=None) -> tuple:
    """Optimize conversation turns. Returns (cleaned_turns, OptReport).

    mode: "rules" | "llm" | "both". "llm" and "both" both run the mechanical
    rules first, then the local LLM clean (the spec's Mode 2 / Mode 3).
    llm_clean_fn(text) -> cleaned text; may raise on local-server failure
    (the LLM stage then falls back to the rules-only output).
    """
    settings = settings or OptSettings()
    mode = (mode or settings.mode).lower()
    report = OptReport(mode=mode)
    report.raw_tokens = count_turns(turns)
    report.final_tokens = report.raw_tokens

    if not turns:
        report.skipped = True
        report.skipped_reason = "no content"
        return turns, report

    if report.raw_tokens < MIN_OPTIMIZE_TOKENS:
        report.skipped = True
        report.skipped_reason = (f"below threshold "
                                 f"({report.raw_tokens} < "
                                 f"{MIN_OPTIMIZE_TOKENS} tokens)")
        log.info("Below threshold -- skipping optimization (%d tokens).",
                 report.raw_tokens)
        return turns, report

    # --- Rules passes ---
    work = [dict(t) for t in turns]
    for name, fn in RULES_PASSES:
        work = fn(work, settings, report)
        work, _ = _prune_empty(work, report)
        report.pass_breakdown.append((name, count_turns(work)))
    report.after_rules_tokens = count_turns(work)

    # --- Local LLM clean (modes llm / both) ---
    if mode in ("llm", "both"):
        cleaned = _llm_clean(work, settings, llm_clean_fn, report)
        if cleaned is not None:
            cleaned, _ = _prune_empty(cleaned, report)
            work = cleaned
            report.after_llm_tokens = count_turns(work)
            report.llm_used = True

    report.final_tokens = count_turns(work)
    if 0 < report.final_tokens < TOO_SMALL_TOKENS:
        report.too_small_after = True
        log.warning("Optimized content is only %d tokens — extraction may "
                    "yield little.", report.final_tokens)

    log.info("Optimization (%s): %d -> %d tokens (%.1f%% reduction).",
             mode, report.raw_tokens, report.final_tokens,
             report.reduction_pct)
    return work, report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(report: OptReport, *, per_pass: bool = True) -> list:
    if report.skipped:
        return [f"Token optimization:   skipped ({report.skipped_reason})"]

    lines = ["Token optimization:"]
    lines.append(f"  Raw:                {report.raw_tokens:,} tokens")
    if per_pass:
        prev = report.raw_tokens
        for name, after in report.pass_breakdown:
            lines.append(f"    {name:<22}{after:>8,}  (-{prev - after:,})")
            prev = after
    lines.append(f"  After rules:        {report.after_rules_tokens:,} tokens")
    if report.after_llm_tokens is not None:
        lines.append(f"  After local LLM:    "
                     f"{report.after_llm_tokens:,} tokens")
    if report.llm_fallback:
        lines.append(f"  Local LLM:          fallback to rules-only "
                     f"({report.llm_fallback_reason})")
    lines.append(f"  Final:              {report.final_tokens:,} tokens "
                 f"({report.reduction_pct:.1f}% reduction)")
    lines.append(f"  Est. input savings: ${report.cost_saving():.4f} "
                 f"at claude-sonnet-4-6 rates")
    if report.turns_dropped:
        lines.append(f"  Turns dropped:      {report.turns_dropped}")
    return lines


def format_batch_totals(reports: list) -> list:
    """Aggregate optimization totals for an --input-dir batch run."""
    active = [r for r in reports if r and not r.skipped]
    if not active:
        return []
    raw = sum(r.raw_tokens for r in active)
    final = sum(r.final_tokens for r in active)
    saved = raw - final
    pct = (100.0 * saved / raw) if raw else 0.0
    cost = saved / 1_000_000 * SONNET_INPUT_RATE
    return [
        "Batch optimization:",
        f"  Files optimized:    {len(active)}",
        f"  Tokens:             {raw:,} -> {final:,} ({pct:.1f}% saved)",
        f"  Est. input savings: ${cost:.4f} at claude-sonnet-4-6 rates",
    ]
