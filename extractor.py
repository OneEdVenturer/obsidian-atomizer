"""Builds the extraction prompt, sends the conversation to the runtime LLM,
and returns parsed notes.

The system prompt lives in templates/system_prompt.md so extraction behavior
can be tuned without touching code. Placeholder tokens ({session_name},
{source_file}, {source_format}, {created}) are substituted with simple string
replacement — NOT str.format() — so literal braces in the template's YAML
and examples are safe.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import optimizer
from llm_client import LLMClient, LocalClient, UsageTracker
from splitter import Note, split_llm_output

log = logging.getLogger("atomizer.extractor")

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "system_prompt.md"
OPTIMIZER_TEMPLATE = (Path(__file__).parent / "templates"
                      / "optimizer_prompt.md")


def _load_optimizer_prompt() -> str:
    return OPTIMIZER_TEMPLATE.read_text(encoding="utf-8")


def _make_local_cleaner(opt: "optimizer.OptSettings"):
    """Build a lazy, local-only LLM cleaning function for the optimizer.

    The local client is constructed on first use, so trivially small inputs
    (which skip the LLM stage) never attempt a network connection. Returns
    None when the optimizer is configured for a non-local provider — the
    optimizer's LLM stage NEVER calls the paid Anthropic API.
    """
    if opt.llm_provider != "local":
        log.warning("optimization.llm_provider is '%s', not 'local' — the "
                    "optimizer only uses local models; skipping LLM clean.",
                    opt.llm_provider)
        return None

    system_prompt = _load_optimizer_prompt()
    state: dict = {}

    def clean(text: str) -> str:
        client = state.get("client")
        if client is None:
            client = LocalClient(
                model=opt.resolved_model(),
                max_tokens=opt.llm_max_tokens,
                usage=UsageTracker(),
                endpoint=opt.llm_endpoint,
                timeout=opt.llm_timeout,
            )
            state["client"] = client
        return client.complete(system=system_prompt, user_content=text).text

    return clean


def optimize_conversation(turns: list, config: dict, mode_override: str | None):
    """Run the pre-send optimizer on a conversation's turns.

    Returns (optimized_turns, OptReport). Called after parsing but before the
    extraction prompt is built (see atomizer.process_conversation).
    """
    opt = optimizer.OptSettings.from_config(config)
    mode = (mode_override or opt.mode).lower()
    if not opt.enabled:
        report = optimizer.OptReport(
            mode=mode, enabled=False, skipped=True,
            skipped_reason="disabled in config",
            raw_tokens=optimizer.count_turns(turns))
        report.final_tokens = report.raw_tokens
        return turns, report
    cleaner = _make_local_cleaner(opt) if mode in ("llm", "both") else None
    return optimizer.optimize(turns, mode=mode, settings=opt,
                              llm_clean_fn=cleaner)


@dataclass
class SessionMeta:
    session_name: str
    source_file: str
    source_format: str
    created: str  # YYYY-MM-DD


def load_system_prompt(meta: SessionMeta,
                       template_path: Path = DEFAULT_TEMPLATE) -> str:
    template = template_path.read_text(encoding="utf-8")
    replacements = {
        "{session_name}": meta.session_name,
        "{source_file}": meta.source_file,
        "{source_format}": meta.source_format,
        "{created}": meta.created,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def format_transcript(turns: list) -> str:
    """Render normalized turns into a transcript the LLM can analyze."""
    lines = ["<conversation>"]
    for i, turn in enumerate(turns, 1):
        role = turn["role"].upper()
        ts = f" timestamp={turn['timestamp']}" if turn.get("timestamp") else ""
        lines.append(f'<turn n="{i}" role="{role}"{ts}>')
        lines.append(turn["content"])
        lines.append("</turn>")
    lines.append("</conversation>")
    return "\n".join(lines)


def extract_notes(client: LLMClient, turns: list, meta: SessionMeta,
                  template_path: Path = DEFAULT_TEMPLATE,
                  chunk_info: str | None = None) -> list[Note]:
    """Run one extraction pass over a set of turns. Returns parsed notes."""
    system = load_system_prompt(meta, template_path)
    transcript = format_transcript(turns)

    user_content = transcript
    if chunk_info:
        user_content = (
            f"NOTE: This is {chunk_info} of a larger conversation that was "
            f"split for length. Extract atoms from this chunk only; some "
            f"context may continue beyond its boundaries.\n\n{transcript}"
        )

    log.info(
        "Sending %d turn(s) to the LLM for extraction%s...",
        len(turns), f" ({chunk_info})" if chunk_info else "",
    )
    response = client.complete(system=system, user_content=user_content)
    notes = split_llm_output(response.text)
    log.info("Extraction pass produced %d note(s).", len(notes))
    return notes
