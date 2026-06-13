"""Plain text parser (.txt freeform paste).

Best-effort speaker turn detection using the same engine as the markdown
parser. If fewer than two turns are detected, the entire content is treated
as a single human-authored block.
"""

import logging

from parsers.markdown_parser import clean_content, extract_turns

log = logging.getLogger("atomizer.parsers.plaintext")


def parse(text: str) -> list:
    """Returns [(session_name, turns)]."""
    turns = extract_turns(text)

    if len(turns) < 2:
        log.info(
            "Plaintext parser: no speaker turns detected — treating entire "
            "content as a single block."
        )
        content = clean_content(text)
        turns = (
            [{"role": "human", "content": content, "timestamp": None}]
            if content else []
        )
    else:
        log.info("Plaintext parser extracted %d turn(s)", len(turns))

    return [(None, turns)]
