"""MAGI personas and protocols, loaded from prompts/*.md.

Edit the markdown files to tune the council — this module only loads and
composes them. Members receive persona (+ mode preamble) + phase protocol as
the system prompt; the evidence packet arrives as the user prompt.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text()


MELCHIOR = _load("melchior")
BALTHASAR = _load("balthasar")
CASPER = _load("casper")
PROTOCOL = _load("protocol")
REBUTTAL_PROTOCOL = _load("rebuttal")
PLAN_PREAMBLE = _load("plan")

PERSONAS = {
    "melchior": MELCHIOR,
    "balthasar": BALTHASAR,
    "casper": CASPER,
}


def system_prompt(role: str, phase: str = "review", mode: str = "code") -> str:
    """Compose the system prompt: persona, then the plan preamble when the
    subject is a proposal document, then the protocol for the phase."""
    parts = [PERSONAS[role]]
    if mode == "plan":
        parts.append(PLAN_PREAMBLE)
    parts.append(REBUTTAL_PROTOCOL if phase == "rebuttal" else PROTOCOL)
    return "\n".join(parts)
