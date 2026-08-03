"""MAGI council: evidence packet, deliberation, rebuttal, asymmetric merge.

Two rounds: members review independently, then cross-examine each other's
findings at the finding level. merge() is provisional without rebuttals and
final with them.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .backends import BackendError, ClaudeCli, CodexCli, GeminiCli
from .personas import PERSONAS, PROTOCOL, REBUTTAL_PROTOCOL

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


REBUTTAL_SCHEMA = {
    "type": "object",
    "properties": {
        "updated_verdict": {
            "type": "string",
            "enum": ["APPROVE", "REQUEST_CHANGES", "ABSTAIN", "UNCHANGED"],
        },
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "position": {
                        "type": "string",
                        "enum": ["ACCEPT", "PARTIALLY_ACCEPT", "CHALLENGE", "OUT_OF_SCOPE"],
                    },
                    "reason": {"type": "string"},
                    "additional_evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["finding_id", "position", "reason", "additional_evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["updated_verdict", "responses"],
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


@dataclass
class MemberRebuttal:
    role: str
    backend: str
    updated_verdict: str = "UNCHANGED"
    responses: list[dict] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None


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


# --- rebuttal round -----------------------------------------------------------

def rebuttal_roles(council: dict[str, object], reviews: list[MemberReview]) -> list[str]:
    """Members that participate in the rebuttal: online, with someone else's
    findings to respond to."""
    by_role = {r.role: r for r in reviews}
    roles = []
    for role in council:
        own = by_role.get(role)
        if own is None or own.verdict == "OFFLINE":
            continue
        if any(rr.findings for rr in reviews if rr.role != role):
            roles.append(role)
    return roles


def _rebuttal_prompt(packet: str, own: MemberReview, others: list[dict]) -> str:
    return (
        f"{packet}\n\n"
        "=== YOUR ORIGINAL REVIEW (reference only) ===\n"
        f"{json.dumps(own.review or {}, indent=2)}\n\n"
        "=== FINDINGS TO RESPOND TO ===\n"
        f"{json.dumps(others, indent=2)}"
    )


async def rebut_member(
    role: str,
    backend,
    packet: str,
    reviews: list[MemberReview],
    repo: Path,
    timeout: float = 600.0,
) -> MemberRebuttal:
    own = next(r for r in reviews if r.role == role)
    others = [
        {"author": rr.role, **f}
        for rr in reviews if rr.role != role
        for f in rr.findings
    ]
    system = PERSONAS[role] + "\n" + REBUTTAL_PROTOCOL
    try:
        r = await backend.ask(
            _rebuttal_prompt(packet, own, others),
            system=system,
            schema=REBUTTAL_SCHEMA,
            cwd=repo,
            timeout=timeout,
        )
    except BackendError as e:
        return MemberRebuttal(role=role, backend=backend.name, error=str(e))
    data = r.data if isinstance(r.data, dict) else {}
    return MemberRebuttal(
        role,
        backend.name,
        data.get("updated_verdict", "UNCHANGED"),
        data.get("responses", []),
        None,
        r.duration_s,
        r.cost_usd,
    )


async def rebut(
    council: dict[str, object],
    packet: str,
    reviews: list[MemberReview],
    repo: Path,
    timeout: float = 600.0,
) -> list[MemberRebuttal]:
    roles = rebuttal_roles(council, reviews)
    return list(await asyncio.gather(
        *(rebut_member(role, council[role], packet, reviews, repo, timeout) for role in roles)
    ))


def merge(
    reviews: list[MemberReview],
    rebuttals: list[MemberRebuttal] | None = None,
) -> dict:
    """Asymmetric merge — provisional without rebuttals, final with them.

    - a confirmed blocking finding vetoes approval, regardless of votes
    - a finding challenged by every responder (and supported by none) is
      DISPUTED: it stops vetoing, but a disputed *blocking* finding escalates
      to HUMAN_REVIEW rather than allowing approval
    - members may update their verdict after the rebuttal
    - non-blocking objections need two members (or a human) to block
    - OFFLINE/ABSTAIN thin the quorum toward HUMAN_REVIEW, never toward APPROVE
    """
    author: dict[str, str] = {}
    findings: dict[str, dict] = {}
    for r in reviews:
        for f in r.findings:
            fid = f.get("id", "?")
            author.setdefault(fid, r.role)
            findings.setdefault(fid, f)

    votes = {r.role: r.verdict for r in reviews}
    supports: dict[str, list[str]] = defaultdict(list)
    challenges: dict[str, list[str]] = defaultdict(list)
    for rb in rebuttals or []:
        if rb.error:
            continue  # failed rebuttal never weakens a finding
        if rb.updated_verdict not in ("", "UNCHANGED"):
            votes[rb.role] = rb.updated_verdict
        for resp in rb.responses:
            fid = resp.get("finding_id", "")
            if fid not in findings or rb.role == author.get(fid):
                continue  # a member cannot support its own finding
            position = resp.get("position")
            if position in ("ACCEPT", "PARTIALLY_ACCEPT"):
                supports[fid].append(rb.role)
            elif position == "CHALLENGE":
                challenges[fid].append(rb.role)

    def disputed(fid: str) -> bool:
        return bool(challenges[fid]) and not supports[fid]

    def label(fid: str) -> str:
        return f"{fid} {findings[fid].get('title', '')}".strip()

    blocking_ids = [fid for fid, f in findings.items() if f.get("severity") == "blocking"]
    blocking_confirmed = [label(fid) for fid in blocking_ids if not disputed(fid)]
    disputed_findings = [label(fid) for fid in findings if disputed(fid)]

    online = [role for role, v in votes.items() if v != "OFFLINE"]
    approvals = sum(votes[role] == "APPROVE" for role in online)
    objections = sum(votes[role] == "REQUEST_CHANGES" for role in online)

    if blocking_confirmed:
        rec = "REQUEST_CHANGES"
    elif any(disputed(fid) for fid in blocking_ids):
        rec = "HUMAN_REVIEW"
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
        "blocking_findings": blocking_confirmed,
        "disputed_findings": disputed_findings,
        "votes": votes,
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

    rebuttals: list[MemberRebuttal] = []
    if rebuttal_roles(council, reviews):
        print("=== REBUTTAL ROUND ===")
        rebuttals = await rebut(council, packet, reviews, repo)
        for rb in rebuttals:
            print(f"--- {rb.role.upper()} verdict: {rb.updated_verdict} ({rb.duration_s:.0f}s)")
            if rb.error:
                print(f"    rebuttal failed: {rb.error}")
            for resp in rb.responses:
                print(f"    {resp['position']:16} {resp['finding_id']}  {resp['reason'][:120]}")
        print()

    print("=== MERGED ===")
    print(json.dumps(merge(reviews, rebuttals), indent=2))


if __name__ == "__main__":
    import sys

    repo_arg = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    task_arg = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(_main(repo_arg, task_arg))
