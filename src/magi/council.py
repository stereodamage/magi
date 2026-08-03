"""MAGI council: evidence packet, parallel deliberation, provisional merge.

Round 2 (finding-level rebuttal) comes next; merge() rules are provisional
until then.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .backends import BackendError, ClaudeCli, CodexCli, GeminiCli
from .personas import PERSONAS, PROTOCOL

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "category": {"type": "string"},
        "severity": {"type": "string", "enum": ["blocking", "high", "medium", "low"]},
        "confidence": {"type": "number"},
        "file": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "trigger": {"type": "string"},
        "observed_failure": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "violated_contract": {"type": "string"},
        "suggested_direction": {"type": "string"},
        "verification": {"type": "string"},
    },
    "required": [
        "id", "title", "category", "severity", "confidence", "file",
        "start_line", "end_line", "trigger", "observed_failure", "evidence",
        "violated_contract", "suggested_direction", "verification",
    ],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "ABSTAIN"]},
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
        "unverified_hypotheses": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "findings", "unverified_hypotheses", "questions", "residual_risks"],
    "additionalProperties": False,
}


@dataclass
class MemberReview:
    role: str
    backend: str
    verdict: str  # APPROVE | REQUEST_CHANGES | ABSTAIN | OFFLINE
    review: dict | None = None
    error: str | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None

    @property
    def findings(self) -> list[dict]:
        return (self.review or {}).get("findings", [])


def default_council() -> dict[str, object]:
    """Per-member model/effort assignment — adjust freely.

    Melchior sits on a different model family than the other two on purpose:
    correctness findings shouldn't share blind spots with the members judging
    them. GeminiCli exists as a stub for a future third family.
    """
    return {
        "melchior": CodexCli(model="gpt-5.6-sol", effort="xhigh"),
        "balthasar": ClaudeCli(model="claude-opus-5", effort="xhigh"),
        "casper": ClaudeCli(model="claude-fable-5", effort="xhigh"),
    }


def is_git_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"


def repo_branch(repo: Path) -> str:
    branch = _git(repo, "branch", "--show-current").strip()
    if branch:
        return branch
    return _git(repo, "rev-parse", "--short", "HEAD").strip() or "-"  # detached


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return r.stdout


def build_packet(repo: Path, task: str | None = None) -> str:
    diff = _git(repo, "diff", "HEAD")
    scope = "uncommitted changes (working tree vs HEAD)"
    if not diff.strip():
        diff = _git(repo, "show", "HEAD", "--patch")
        scope = "last commit"
    task_text = task or (
        "(no task description provided — infer intent conservatively and "
        "raise missing context through `questions`)"
    )
    return (
        "=== EVIDENCE PACKET ===\n\n"
        f"TASK / ACCEPTANCE CRITERIA:\n{task_text}\n\n"
        f"CHANGE UNDER REVIEW — {scope}:\n```diff\n{diff}```\n\n"
        "The full repository is available read-only in your working "
        "directory. Inspect surrounding code, callers, and tests before "
        "judging; never judge from the diff alone."
    )


async def review_member(
    role: str,
    backend,
    packet: str,
    repo: Path,
    timeout: float = 600.0,
) -> MemberReview:
    system = PERSONAS[role] + "\n" + PROTOCOL
    try:
        r = await backend.ask(
            packet, system=system, schema=REVIEW_SCHEMA, cwd=repo, timeout=timeout
        )
    except BackendError as e:
        return MemberReview(role=role, backend=backend.name, verdict="OFFLINE", error=str(e))
    review = r.data if isinstance(r.data, dict) else None
    verdict = (review or {}).get("verdict", "ABSTAIN")
    return MemberReview(role, backend.name, verdict, review, None, r.duration_s, r.cost_usd)


async def deliberate(
    council: dict[str, object],
    packet: str,
    repo: Path,
    timeout: float = 600.0,
) -> list[MemberReview]:
    return list(await asyncio.gather(
        *(review_member(role, b, packet, repo, timeout) for role, b in council.items())
    ))


def merge(reviews: list[MemberReview]) -> dict:
    """Provisional asymmetric merge (pre-rebuttal).

    - any blocking finding vetoes approval, regardless of votes
    - non-blocking objections need two members (or a human) to block
    - OFFLINE/ABSTAIN thin the quorum toward HUMAN_REVIEW, never toward APPROVE
    """
    blocking = [
        f"{f.get('id', '?')} {f.get('title', '')}".strip()
        for r in reviews
        for f in r.findings
        if f.get("severity") == "blocking"
    ]
    online = [r for r in reviews if r.verdict != "OFFLINE"]
    approvals = sum(r.verdict == "APPROVE" for r in online)
    objections = sum(r.verdict == "REQUEST_CHANGES" for r in online)

    if blocking:
        rec = "REQUEST_CHANGES"
    elif objections >= 2:
        rec = "REQUEST_CHANGES"
    elif objections == 1:
        rec = "HUMAN_REVIEW"
    elif approvals >= 2:
        rec = "APPROVE"
    else:
        rec = "HUMAN_REVIEW"

    return {
        "recommendation": rec,
        "blocking_findings": blocking,
        "votes": {r.role: r.verdict for r in reviews},
        "offline": [r.role for r in reviews if r.verdict == "OFFLINE"],
    }


# --- demo entry point --------------------------------------------------------

async def _main(repo: Path, task: str | None) -> None:
    council = default_council()
    packet = build_packet(repo, task)
    names = ", ".join(f"{role.upper()}({b.name})" for role, b in council.items())
    print(f"MAGI council convened: {names}")
    print(f"packet: {len(packet)} chars\n")

    reviews = await deliberate(council, packet, repo)

    for r in reviews:
        cost = f" ${r.cost_usd:.2f}" if r.cost_usd else ""
        print(f"--- {r.role.upper()} [{r.backend}] {r.verdict} ({r.duration_s:.0f}s{cost})")
        if r.error:
            print(f"    offline: {r.error}")
        for f in r.findings:
            loc = f" {f['file']}:{f['start_line']}" if f.get("file") else ""
            print(f"    [{f['severity']:8}] {f['id']} {f['title']} (conf {f['confidence']:.2f}){loc}")
            print(f"               trigger: {f['trigger'][:150]}")
        rv = r.review or {}
        for key in ("unverified_hypotheses", "questions", "residual_risks"):
            for item in rv.get(key, []):
                print(f"    ({key[:-1]}) {item[:150]}")
        print()

    print("=== MERGED (provisional) ===")
    print(json.dumps(merge(reviews), indent=2))


if __name__ == "__main__":
    import sys

    repo_arg = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    task_arg = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(_main(repo_arg, task_arg))
