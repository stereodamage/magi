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

from .backends import ClaudeCli, CodexCli, GeminiCli
from .personas import system_prompt

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
    # the reply as the member wrote it, before any parsing, and the vendor
    # envelope around it (usage, cost, session id) when the CLI returns one.
    # save_run keeps both: `review` is what the protocol used, `raw_text` is
    # what the model actually said, and the two differ in the interesting cases.
    raw_text: str = ""
    raw_envelope: dict | None = None

    @property
    def findings(self) -> list[dict]:
        """Findings, renumbered `<role letter><n>` — M1, B2, C3.

        Members number their own findings and two of them can pick the same
        id for different bugs. The merge keys on the id, so a collision drops
        a finding, and a dropped blocking finding loses its veto. Renumbering
        makes the id unique by construction. Every reader — merge, rebuttal
        prompt, both renderers — goes through this property, so the id a
        member sees is the id the merge uses.
        """
        raw = (self.review or {}).get("findings", [])
        return [{**f, "id": f"{self.role[0].upper()}{i}"} for i, f in enumerate(raw, 1)]


@dataclass
class MemberRebuttal:
    role: str
    backend: str
    updated_verdict: str = "UNCHANGED"
    responses: list[dict] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None
    raw_text: str = ""  # as in MemberReview: the reply before parsing
    raw_envelope: dict | None = None


@dataclass(frozen=True)
class Finding:
    """Read-only view over one schema-validated finding, for the renderers.

    The wire format stays the raw dict — it is what the member returned and
    what render_json writes back out. This view exists so the TUI ticker and
    the text report read fields by name in one place instead of each reaching
    into the dict. Layout stays with each renderer; they differ on purpose.
    """

    id: str
    title: str
    severity: str
    confidence: float
    location: str  # "path:line", empty when the member named no file
    trigger: str

    @classmethod
    def of(cls, f: dict) -> Finding:
        return cls(
            id=f.get("id", "?"),
            title=f.get("title", ""),
            severity=f.get("severity", ""),
            confidence=float(f.get("confidence", 0.0)),
            location=f"{f['file']}:{f['start_line']}" if f.get("file") else "",
            trigger=f.get("trigger", ""),
        )


def finding_authors(reviews: list[MemberReview]) -> dict[str, str]:
    """finding id → the role that filed it. The first filer wins a collision."""
    author: dict[str, str] = {}
    for r in reviews:
        for f in r.findings:
            author.setdefault(f.get("id", "?"), r.role)
    return author


# gpt-5.6-sol's max_context_window, per `codex debug models`. The model's
# own default is 272000; a review packet plus the repo it reads around fills
# that. Tied to the model on the line below, because another model's ceiling
# is another number and asking for more than the ceiling is a 400.
_SOL_WINDOW = 872_000


def default_council() -> dict[str, object]:
    """Per-member model/effort assignment — adjust freely.

    Melchior sits on a different model family than the other two on purpose:
    correctness findings shouldn't share blind spots with the members judging
    them. GeminiCli exists as a stub for a future third family.
    """
    return {
        "melchior": CodexCli(model="gpt-5.6-sol", effort="xhigh",
                             context_window=_SOL_WINDOW),
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


def _git_ok(repo: Path, *args: str) -> tuple[str, str]:
    """(stdout, error). error is git's stderr when the command failed."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if r.returncode == 0:
        return r.stdout, ""
    return r.stdout, r.stderr.strip() or f"git {args[0]} exited {r.returncode}"


def _git(repo: Path, *args: str) -> str:
    """stdout only — for commands whose failure carries no information, and
    for `diff --no-index`, which exits 1 whenever the two files differ."""
    return _git_ok(repo, *args)[0]


_UNTRACKED_CAP = 100_000  # bytes per untracked file
_PACKET_CAP = 2_000_000  # bytes of diff per packet, across all sources
TIMEOUT = 900.0  # seconds a member may take per round

# Prose, not code. A plan or a design note read as a diff produces findings
# about the document rather than about the change, so it stays out of scope.
# `magi plan DOC` reviews such a document on purpose, with the right protocol.
_SCOPE = (".", ":(exclude)docs/")


def _truncate(diff: str, cap: int = _PACKET_CAP) -> str:
    if len(diff) <= cap:
        return diff
    # the marker lands at the end of the diff, so it names the whole scope
    return f"{diff[:cap]}\n... truncated at {cap} bytes — this change is incomplete\n"


def _untracked_diffs(repo: Path, budget: int = _PACKET_CAP) -> str:
    """New-file diffs for untracked files, read-only via git diff --no-index.

    Respects .gitignore. Two limits: one stray data dump cannot flood the
    packet (per-file cap), and a repo that fails to ignore its build output
    cannot either — thousands of small files still stop at the budget."""
    parts = []
    used = 0
    listed = _git(repo, "ls-files", "--others", "--exclude-standard", "--", *_SCOPE)
    for name in listed.splitlines():
        if used >= budget:
            parts.append(f"# untracked scan stopped: {budget} byte budget reached\n")
            break
        try:
            size = (repo / name).stat().st_size
        except OSError:
            continue
        if size > _UNTRACKED_CAP:
            parts.append(f"# untracked file skipped ({size} bytes > {_UNTRACKED_CAP}): {name}\n")
            continue
        one = _git(repo, "diff", "--no-index", "--", "/dev/null", name)
        used += len(one)
        parts.append(one)
    return "".join(parts)


def build_packet(repo: Path, task: str | None = None) -> str:
    diff, err = _git_ok(repo, "diff", "HEAD", "--", *_SCOPE)
    diff += _untracked_diffs(repo, _PACKET_CAP - len(diff))
    scope = "uncommitted changes (working tree vs HEAD, untracked files included)"
    if not diff.strip():
        diff, err = _git_ok(repo, "show", "HEAD", "--patch", "--", *_SCOPE)
        scope = "last commit"
    if not diff.strip():
        # an empty packet asks the council to review nothing, and nothing
        # reads as APPROVE — refuse it instead of passing the gate
        raise ValueError(f"nothing to review in {repo}: {err or 'no changes outside docs/'}")
    scope += "; docs/ is out of scope — do not report on documents"
    diff = _truncate(diff)
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


def build_plan_packet(doc: Path, context: str | None = None) -> str:
    context_text = context or (
        "(no context provided — infer the goals conservatively and raise "
        "missing context through `questions`)"
    )
    return (
        "=== PROPOSAL PACKET ===\n\n"
        f"CONTEXT / GOALS / CONSTRAINTS:\n{context_text}\n\n"
        f"PROPOSAL DOCUMENT — {doc.name}:\n{_truncate(doc.read_text())}\n\n"
        "The proposal's directory is available read-only in your working "
        "directory; inspect existing code there when the proposal refers "
        "to it."
    )


async def review_member(
    role: str,
    backend,
    packet: str,
    repo: Path,
    timeout: float = TIMEOUT,
    mode: str = "code",
    on_progress=None,
) -> MemberReview:
    system = system_prompt(role, "review", mode)
    try:
        r = await backend.ask(
            packet, system=system, schema=REVIEW_SCHEMA, cwd=repo, timeout=timeout,
            on_progress=on_progress,
        )
    # every failure is this member's failure, not the council's: a vendor CLI
    # that returns a list where a dict belongs, or bytes that do not decode,
    # must take one seat OFFLINE and leave the other two reviewing
    except Exception as e:  # CancelledError is a BaseException — it still propagates
        return MemberReview(
            role=role, backend=backend.name, verdict="OFFLINE", error=str(e),
            raw_text=getattr(e, "raw", ""),  # a reply that failed to parse is still evidence
        )
    review = r.data if isinstance(r.data, dict) else None
    verdict = (review or {}).get("verdict", "ABSTAIN")
    return MemberReview(
        role, backend.name, verdict, review, None, r.duration_s, r.cost_usd,
        raw_text=r.text, raw_envelope=r.raw,
    )


async def deliberate(
    council: dict[str, object],
    packet: str,
    repo: Path,
    timeout: float = TIMEOUT,
    mode: str = "code",
) -> list[MemberReview]:
    return list(await asyncio.gather(
        *(review_member(role, b, packet, repo, timeout, mode) for role, b in council.items())
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
        # own.findings, not own.review["findings"]: both blocks must show the
        # renumbered ids, or the member answers with an id no one recognises
        f"{json.dumps({**(own.review or {}), 'findings': own.findings}, indent=2)}\n\n"
        "=== FINDINGS TO RESPOND TO ===\n"
        f"{json.dumps(others, indent=2)}"
    )


async def rebut_member(
    role: str,
    backend,
    packet: str,
    reviews: list[MemberReview],
    repo: Path,
    timeout: float = TIMEOUT,
    mode: str = "code",
    on_progress=None,
) -> MemberRebuttal:
    own = next(r for r in reviews if r.role == role)
    others = [
        {"author": rr.role, **f}
        for rr in reviews if rr.role != role
        for f in rr.findings
    ]
    system = system_prompt(role, "rebuttal", mode)
    try:
        r = await backend.ask(
            _rebuttal_prompt(packet, own, others),
            system=system,
            schema=REBUTTAL_SCHEMA,
            cwd=repo,
            timeout=timeout,
            on_progress=on_progress,
        )
    except Exception as e:  # as in review_member: one seat fails, not the council
        return MemberRebuttal(
            role=role, backend=backend.name, error=str(e), raw_text=getattr(e, "raw", ""),
        )
    data = r.data if isinstance(r.data, dict) else {}
    return MemberRebuttal(
        role,
        backend.name,
        data.get("updated_verdict", "UNCHANGED"),
        data.get("responses", []),
        None,
        r.duration_s,
        r.cost_usd,
        raw_text=r.text,
        raw_envelope=r.raw,
    )


async def rebut(
    council: dict[str, object],
    packet: str,
    reviews: list[MemberReview],
    repo: Path,
    timeout: float = TIMEOUT,
    mode: str = "code",
) -> list[MemberRebuttal]:
    roles = rebuttal_roles(council, reviews)
    return list(await asyncio.gather(
        *(rebut_member(role, council[role], packet, reviews, repo, timeout, mode)
          for role in roles)
    ))


def merge(
    reviews: list[MemberReview],
    rebuttals: list[MemberRebuttal] | None = None,
    mode: str = "code",
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
    author = finding_authors(reviews)
    findings: dict[str, dict] = {}
    for r in reviews:
        for f in r.findings:
            findings.setdefault(f.get("id", "?"), f)

    votes = {r.role: r.verdict for r in reviews}
    supports: dict[str, list[str]] = defaultdict(list)
    challenges: dict[str, list[str]] = defaultdict(list)
    responders: set[str] = set()
    for rb in rebuttals or []:
        if rb.error:
            continue  # failed rebuttal never weakens a finding
        responders.add(rb.role)
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
        # every responder must challenge, none may support; an omitted or
        # OUT_OF_SCOPE response keeps the finding confirmed (fail-safe)
        eligible = responders - {author.get(fid)}
        return bool(eligible) and not supports[fid] and eligible <= set(challenges[fid])

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

    if mode == "plan" and rec == "APPROVE":
        rec = "HUMAN_REVIEW"  # a plan is approved by a person, never by the council

    return {
        "recommendation": rec,
        "mode": mode,
        "blocking_findings": blocking_confirmed,
        "disputed_findings": disputed_findings,
        "votes": votes,
        "offline": [r.role for r in reviews if r.verdict == "OFFLINE"],
    }


# --- headless / CI -------------------------------------------------------------

EXIT_CODES = {"APPROVE": 0, "REQUEST_CHANGES": 1, "HUMAN_REVIEW": 2}
EXIT_ERROR = 3


def _progress(emit, role: str):
    """A sink for one member's CLI output.

    The panels pulse whether or not the CLI is doing anything. A line that
    arrives from the CLI is the only live proof that it still is.
    """
    return lambda line: emit("progress", (role, line))


async def _stream(coros: list, emit, kind: str) -> list:
    """Run the coroutines together and emit each result as it lands.

    The finally clause carries the cancellation: when the operator stops the
    council, the members still in flight are cancelled too, and cancelling
    them kills their CLI processes. Without it they keep burning tokens with
    no one left to receive the answer.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    done = []
    try:
        for fut in asyncio.as_completed(tasks):
            r = await fut
            done.append(r)
            emit(kind, r)
    finally:
        for t in tasks:
            t.cancel()
    return done


async def convene(
    council: dict[str, object],
    repo: Path,
    task: str | None = None,
    timeout: float = TIMEOUT,
    on_event=None,
    packet: str | None = None,
    mode: str = "code",
) -> tuple[list[MemberReview], list[MemberRebuttal], dict]:
    """Full protocol: packet → parallel reviews → rebuttal → final merge.

    on_event(kind, payload) streams progress as it happens:
    ("packet", str) → ("review", MemberReview)… → ("rebuttal_start", [roles])
    → ("rebuttal", MemberRebuttal)… → ("merged", dict).

    This is the one implementation of the protocol. Both front ends drive it:
    the TUI through on_event, run_headless through its stderr emit.
    """
    emit = on_event or (lambda kind, payload: None)
    packet = packet or build_packet(repo, task)
    emit("packet", packet)
    reviews = await _stream(
        [review_member(role, b, packet, repo, timeout, mode, _progress(emit, role))
         for role, b in council.items()],
        emit,
        "review",
    )
    roles = rebuttal_roles(council, reviews)
    rebuttals: list[MemberRebuttal] = []
    if roles:
        emit("rebuttal_start", roles)
        rebuttals = await _stream(
            [rebut_member(role, council[role], packet, reviews, repo, timeout, mode,
                          _progress(emit, role))
             for role in roles],
            emit,
            "rebuttal",
        )
    merged = merge(reviews, rebuttals, mode)
    emit("merged", merged)
    return reviews, rebuttals, merged


def render_text(
    reviews: list[MemberReview],
    rebuttals: list[MemberRebuttal],
    merged: dict,
) -> str:
    out = []
    for r in reviews:
        cost = f" ${r.cost_usd:.2f}" if r.cost_usd else ""
        out.append(f"--- {r.role.upper()} [{r.backend}] {r.verdict} ({r.duration_s:.0f}s{cost})")
        if r.error:
            out.append(f"    offline: {r.error}")
        for raw in r.findings:
            f = Finding.of(raw)
            loc = f" {f.location}" if f.location else ""
            out.append(f"    [{f.severity:8}] {f.id} {f.title} (conf {f.confidence:.2f}){loc}")
            out.append(f"               trigger: {f.trigger[:150]}")
        rv = r.review or {}
        for key in ("unverified_hypotheses", "questions", "residual_risks"):
            for item in rv.get(key, []):
                out.append(f"    ({key[:-1]}) {item[:150]}")
        out.append("")
    if rebuttals:
        author = finding_authors(reviews)
        out.append("=== REBUTTAL ROUND ===")
        for rb in rebuttals:
            out.append(f"--- {rb.role.upper()} verdict: {rb.updated_verdict} ({rb.duration_s:.0f}s)")
            if rb.error:
                out.append(f"    rebuttal failed: {rb.error}")
            for resp in rb.responses:
                fid = resp["finding_id"]
                filed_by = author.get(fid, "?")
                out.append(
                    f"    {resp['position']:16} {fid} (by {filed_by})"
                    f"  {resp['reason'][:120]}"
                )
        out.append("")
    out.append("=== MERGED ===")
    out.append(json.dumps(merged, indent=2))
    return "\n".join(out)


def render_json(
    reviews: list[MemberReview],
    rebuttals: list[MemberRebuttal],
    merged: dict,
    packet: str | None = None,
) -> str:
    """The full result as JSON. With `packet`, the record is self-contained:
    the exact prompt the members read next to the replies they wrote."""
    from dataclasses import asdict

    def one(r: MemberReview) -> dict:
        d = asdict(r)
        if d["review"] is not None:  # the ids the merge and the ticker used
            d["review"] = {**d["review"], "findings": r.findings}
        return d

    out = {
        **merged,
        "reviews": [one(r) for r in reviews],
        "rebuttals": [asdict(rb) for rb in rebuttals],
    }
    if packet is not None:
        out["packet"] = packet
    return json.dumps(out, indent=2)


def save_run(
    repo: Path,
    reviews: list[MemberReview],
    rebuttals: list[MemberRebuttal],
    merged: dict,
    packet: str | None = None,
) -> Path:
    """Write the whole deliberation to .magi/runs/, and return the path.

    Every run is kept, packet and raw replies included. One overwritten file
    is not a corpus, and the raw text is the part worth studying: what the
    member wrote, next to what the protocol made of it.

    The directory ignores itself, so the logs reach neither git nor the
    evidence packet of the next run.
    """
    from datetime import UTC, datetime

    runs = repo / ".magi" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs.parent / ".gitignore").write_text("*\n")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = runs / f"{stamp}-{merged['recommendation']}.json"
    path.write_text(render_json(reviews, rebuttals, merged, packet))
    link = runs.parent / "last-run.json"  # stable path, so `cat` needs no timestamp
    try:
        link.unlink(missing_ok=True)
        link.symlink_to(Path("runs") / path.name)
    except OSError:
        pass  # ponytail: the timestamped file is the record, the alias is a convenience
    return path


def run_headless(
    council: dict[str, object],
    repo: Path,
    task: str | None = None,
    as_json: bool = False,
    timeout: float = TIMEOUT,
    packet: str | None = None,
    mode: str = "code",
) -> int:
    """Run the full protocol without a TUI. Returns the process exit code:
    0 APPROVE · 1 REQUEST_CHANGES · 2 HUMAN_REVIEW · 3 error.

    Phase progress streams to stderr as members finish, so a piped or CI run
    stays observable while stdout waits for the final report."""
    import sys
    import time

    if packet is None and not is_git_repo(repo):
        print(f"magi: not a git repository: {repo}", file=sys.stderr)
        return EXIT_ERROR
    names = ", ".join(f"{role.upper()}({b.name})" for role, b in council.items())
    print(f"MAGI council convened: {names}", file=sys.stderr)
    t0 = time.monotonic()

    sent = ""  # the packet as built, kept for the saved record
    said = {}  # role → when its last progress line was printed

    def emit(kind: str, payload) -> None:
        nonlocal sent
        if kind == "progress":
            role, line = payload
            now = time.monotonic()
            if now - said.get(role, 0.0) < 30.0:
                return  # a status, not a transcript
            said[role] = now
            msg = f"{role.upper()} working: {line[:100]}"
        elif kind == "packet":
            sent = payload
            msg = f"packet built ({len(payload)} chars); review round: {len(council)} members"
        elif kind == "review":
            note = f" — {payload.error}" if payload.error else ""
            msg = (f"{payload.role.upper()} review: {payload.verdict}"
                   f" ({len(payload.findings)} findings, {payload.duration_s:.0f}s){note}")
        elif kind == "rebuttal_start":
            msg = f"rebuttal round: {', '.join(payload)}"
        elif kind == "rebuttal":
            note = f" — {payload.error}" if payload.error else ""
            msg = (f"{payload.role.upper()} rebuttal: {payload.updated_verdict}"
                   f" ({len(payload.responses)} positions, {payload.duration_s:.0f}s){note}")
        elif kind == "merged":
            msg = f"決議 {payload['recommendation']}"
        else:
            return
        print(f"[T+{time.monotonic() - t0:4.0f}s] {msg}", file=sys.stderr, flush=True)

    try:
        reviews, rebuttals, merged = asyncio.run(
            convene(council, repo, task, timeout, on_event=emit, packet=packet, mode=mode)
        )
    except Exception as e:
        # exit 1 means REQUEST_CHANGES to the caller. A crash must never say
        # that: it did not review anything, so it reports EXIT_ERROR.
        print(f"magi: council failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    try:
        print(f"full run: {save_run(repo, reviews, rebuttals, merged, sent)}", file=sys.stderr)
    except OSError as e:  # a read-only checkout must not lose the verdict
        print(f"magi: could not save the run: {e}", file=sys.stderr)
    render = render_json if as_json else render_text
    print(render(reviews, rebuttals, merged))
    return EXIT_CODES.get(merged["recommendation"], EXIT_CODES["HUMAN_REVIEW"])


if __name__ == "__main__":
    import sys

    from .config import load_council

    repo_arg = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    task_arg = sys.argv[2] if len(sys.argv) > 2 else None
    raise SystemExit(run_headless(load_council(repo_arg), repo_arg, task_arg))
