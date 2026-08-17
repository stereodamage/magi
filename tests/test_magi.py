"""Synthetic tests: no live model calls, no network. Fake backends only."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from conftest import FakeBackend, finding, position, rebuttal, review

from magi.backends import ClaudeCli, CodexCli
from magi.council import (
    REBUTTAL_SCHEMA,
    REVIEW_SCHEMA,
    MemberRebuttal,
    MemberReview,
    build_packet,
    deliberate,
    merge,
    rebut,
    rebuttal_roles,
)
from magi.personas import PERSONAS, PROTOCOL, REBUTTAL_PROTOCOL


def run_council(**members):
    return asyncio.run(deliberate(members, "PACKET", Path(".")))


def MR(role, verdict, findings=()):
    return MemberReview(role, "fake", verdict, review(verdict, findings))


# --- command construction ----------------------------------------------------

def test_claude_cmd_flags():
    b = ClaudeCli(model="claude-opus-5", effort="xhigh")
    cmd = b._cmd(system="PERSONA", schema={"type": "object"})
    assert cmd[:2] == ["claude", "-p"]
    for pair in (
        ["--model", "claude-opus-5"],
        ["--effort", "xhigh"],
        ["--setting-sources", ""],  # pristine: no settings/hooks/CLAUDE.md
        ["--append-system-prompt", "PERSONA"],
        ["--allowedTools", "Read,Grep,Glob"],
        ["--json-schema", '{"type": "object"}'],
    ):
        i = cmd.index(pair[0])
        assert cmd[i + 1] == pair[1], pair


def test_claude_cmd_minimal():
    cmd = ClaudeCli(pristine=False, allowed_tools=(), effort=None)._cmd(None, None)
    for flag in ("--setting-sources", "--append-system-prompt", "--allowedTools",
                 "--json-schema", "--effort"):
        assert flag not in cmd


def test_codex_cmd_flags(tmp_path):
    b = CodexCli(model="gpt-5.6-sol", effort="xhigh")
    out, schema = tmp_path / "o.txt", tmp_path / "s.json"
    cmd = b._cmd(out, schema)
    assert cmd[:3] == ["codex", "exec", "-"]  # prompt via stdin
    assert "--ephemeral" in cmd and "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"
    assert "model_reasoning_effort=xhigh" in cmd
    assert "project_doc_max_bytes=0" in cmd  # pristine
    assert cmd[cmd.index("--output-schema") + 1] == str(schema)


def test_finding_view_and_authorship():
    from magi.council import Finding, finding_authors

    raw = finding("high", "M-001")
    f = Finding.of(raw)
    assert (f.id, f.severity, f.confidence) == ("M-001", "high", 0.9)
    assert f.location == "f.py:1"

    nowhere = dict(raw)
    del nowhere["file"]  # members may report a finding with no file
    assert Finding.of(nowhere).location == ""

    assert Finding.of({}).id == "?"  # a malformed finding must not crash a render

    reviews = [
        MR("balthasar", "REQUEST_CHANGES", [finding("low", "B-003")]),
        MR("melchior", "APPROVE", [finding("high", "M-001")]),
    ]
    assert finding_authors(reviews) == {"B1": "balthasar", "M1": "melchior"}


def test_text_report_names_the_finding_author():
    from magi.council import render_text

    reviews = [
        MR("balthasar", "REQUEST_CHANGES", [finding("low", "B-003")]),
        MR("melchior", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("melchior", "codex", "REQUEST_CHANGES",
                       [position("B1", "CHALLENGE", "no path")]),
    ]
    report = render_text(reviews, rebuttals, merge(reviews, rebuttals))
    # a position targets someone else's finding — the report must say whose
    assert "B1 (by balthasar)" in report


async def test_cancel_kills_the_member_process():
    """ctrl+c must not orphan a running claude/codex child."""
    from magi.backends import _run

    marker = "31.4159"  # unique enough to pgrep for

    def alive():
        return subprocess.run(
            ["pgrep", "-f", f"sleep {marker}"], capture_output=True
        ).returncode == 0

    task = asyncio.ensure_future(_run(["sleep", marker], "", 30.0, None))
    for _ in range(40):
        await asyncio.sleep(0.05)
        if alive():
            break
    assert alive(), "child never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        await asyncio.sleep(0.05)
        if not alive():
            break
    assert not alive(), "cancelled member left an orphan process"


# --- schema strictness (OpenAI structured-output rules) ----------------------

def _walk(node):
    yield node
    for v in node.get("properties", {}).values():
        yield from _walk(v)
    if "items" in node:
        yield from _walk(node["items"])


def test_review_schema_strict():
    for node in _walk(REVIEW_SCHEMA):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"].keys())


# --- deliberation ------------------------------------------------------------

def test_deliberate_personas_and_verdicts():
    m = FakeBackend(review(verdict="REQUEST_CHANGES", findings=[finding()]))
    b = FakeBackend(review())
    reviews = run_council(melchior=m, balthasar=b)
    by_role = {r.role: r for r in reviews}
    assert by_role["melchior"].verdict == "REQUEST_CHANGES"
    assert by_role["balthasar"].verdict == "APPROVE"
    # each member got its own persona + shared protocol as system prompt
    assert m.calls[0]["system"].startswith(PERSONAS["melchior"])
    assert PROTOCOL in b.calls[0]["system"]
    assert m.calls[0]["schema"] == REVIEW_SCHEMA
    assert m.calls[0]["prompt"] == "PACKET"


def test_deliberate_offline_member():
    reviews = run_council(melchior=FakeBackend(fail=True), balthasar=FakeBackend(review()))
    by_role = {r.role: r for r in reviews}
    assert by_role["melchior"].verdict == "OFFLINE"
    assert "boom" in by_role["melchior"].error
    assert by_role["balthasar"].verdict == "APPROVE"


def test_deliberate_malformed_review_is_abstain():
    bad = FakeBackend(review=None)
    bad.review = None  # ask() returns data=None
    reviews = run_council(melchior=bad)
    assert reviews[0].verdict == "ABSTAIN"


# --- merge rules -------------------------------------------------------------

def _merged(*verdict_findings):
    reviews = run_council(**{
        role: FakeBackend(review(v, f))
        for role, (v, f) in zip(("melchior", "balthasar", "casper"), verdict_findings)
    })
    return merge(reviews)


def test_blocking_finding_vetoes_even_unanimous_approval():
    got = _merged(("APPROVE", [finding("blocking")]), ("APPROVE", []), ("APPROVE", []))
    assert got["recommendation"] == "REQUEST_CHANGES"
    assert got["blocking_findings"] == ["M1 t"]


def test_unanimous_approval():
    got = _merged(("APPROVE", []), ("APPROVE", []), ("APPROVE", []))
    assert got["recommendation"] == "APPROVE"


def test_single_nonblocking_objection_goes_to_human():
    got = _merged(("REQUEST_CHANGES", [finding("high")]), ("APPROVE", []), ("APPROVE", []))
    assert got["recommendation"] == "HUMAN_REVIEW"


def test_two_objections_request_changes():
    got = _merged(
        ("REQUEST_CHANGES", [finding("high")]),
        ("REQUEST_CHANGES", [finding("high", "B-001")]),
        ("APPROVE", []),
    )
    assert got["recommendation"] == "REQUEST_CHANGES"


def test_offline_thins_quorum_to_human_review():
    reviews = run_council(
        melchior=FakeBackend(fail=True),
        balthasar=FakeBackend(fail=True),
        casper=FakeBackend(review()),
    )
    got = merge(reviews)
    assert got["recommendation"] == "HUMAN_REVIEW"
    assert sorted(got["offline"]) == ["balthasar", "melchior"]


def test_two_online_approvals_with_one_offline_still_approve():
    reviews = run_council(
        melchior=FakeBackend(review()),
        balthasar=FakeBackend(review()),
        casper=FakeBackend(fail=True),
    )
    assert merge(reviews)["recommendation"] == "APPROVE"


# --- rebuttal round ------------------------------------------------------------

def test_rebuttal_roles_skip_offline_and_nothing_to_answer():
    council = {"melchior": None, "balthasar": None, "casper": None}
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),  # only author of findings
        MR("balthasar", "APPROVE"),
        MemberReview("casper", "fake", "OFFLINE", None, "boom"),
    ]
    # melchior has no one else's findings; casper is offline; balthasar responds
    assert rebuttal_roles(council, reviews) == ["balthasar"]


def test_rebut_prompt_carries_own_review_and_others_findings():
    m = FakeBackend(review("REQUEST_CHANGES", [finding("blocking", "M-001")]))
    b = FakeBackend(review("REQUEST_CHANGES", [finding("high", "B-001")]))
    council = {"melchior": m, "balthasar": b}
    reviews = run_council(**council)
    rebuttals = asyncio.run(rebut(council, "PACKET", reviews, Path(".")))
    assert {rb.role for rb in rebuttals} == {"melchior", "balthasar"}
    # melchior's second call: sees B1 as target, own M1 only as reference
    rebut_call = m.calls[1]
    assert rebut_call["schema"] is REBUTTAL_SCHEMA
    assert "FINDINGS TO RESPOND TO" in rebut_call["prompt"]
    assert '"B1"' in rebut_call["prompt"].split("FINDINGS TO RESPOND TO")[1]
    assert "YOUR ORIGINAL REVIEW" in rebut_call["prompt"]
    assert REBUTTAL_PROTOCOL in rebut_call["system"]


def test_disputed_blocking_escalates_to_human():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["recommendation"] == "HUMAN_REVIEW"
    assert got["blocking_findings"] == []
    assert got["disputed_findings"] == ["M1 t"]


def test_supported_blocking_still_vetoes_despite_challenge():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M1", "PARTIALLY_ACCEPT")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["recommendation"] == "REQUEST_CHANGES"
    assert got["blocking_findings"] == ["M1 t"]
    assert got["disputed_findings"] == []


def test_author_cannot_support_own_finding():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("melchior", "fake", "UNCHANGED", [position("M1", "ACCEPT")]),
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["disputed_findings"] == ["M1 t"]
    assert got["recommendation"] == "HUMAN_REVIEW"


def test_updated_verdict_flips_the_vote():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("high")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    # provisional: one objection → HUMAN_REVIEW
    assert merge(reviews)["recommendation"] == "HUMAN_REVIEW"
    # after rebuttal melchior concedes → unanimous APPROVE
    rebuttals = [MemberRebuttal("melchior", "fake", "APPROVE", [])]
    got = merge(reviews, rebuttals)
    assert got["votes"]["melchior"] == "APPROVE"
    assert got["recommendation"] == "APPROVE"


def test_lone_challenge_cannot_disarm_blocking_veto():
    """One member's CHALLENGE while the other responds OUT_OF_SCOPE (or stays
    silent on the finding) must NOT dispute it — unanimity among responders."""
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M1", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M1", "OUT_OF_SCOPE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["disputed_findings"] == []
    assert got["blocking_findings"] == ["M1 t"]
    assert got["recommendation"] == "REQUEST_CHANGES"

    # same with casper omitting a response for M-001 entirely
    rebuttals[1] = MemberRebuttal("casper", "fake", "UNCHANGED", [])
    got = merge(reviews, rebuttals)
    assert got["disputed_findings"] == []
    assert got["recommendation"] == "REQUEST_CHANGES"


def test_missing_binary_becomes_backend_error():
    from magi.backends import BackendError, _run

    with pytest.raises(BackendError, match="cannot start"):
        asyncio.run(_run(["magi-no-such-binary-xyz"], "", 5.0, None))


def test_failed_rebuttal_never_weakens_findings():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", error="timeout",
                       responses=[position("M1", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["blocking_findings"] == ["M1 t"]
    assert got["recommendation"] == "REQUEST_CHANGES"


# --- evidence packet ---------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True, capture_output=True,
        )
    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return tmp_path


# --- headless / CI -------------------------------------------------------------

def test_run_headless_exit_codes_and_json(repo, capsys):
    import json as jsonlib

    from magi.council import EXIT_ERROR, run_headless

    (repo / "a.py").write_text("x = 2\n")  # something to review

    approve = {r: FakeBackend(review()) for r in ("melchior", "balthasar", "casper")}
    assert run_headless(approve, repo, as_json=True) == 0
    out = jsonlib.loads(capsys.readouterr().out)
    assert out["recommendation"] == "APPROVE"
    assert {r["role"] for r in out["reviews"]} == set(approve)

    blocking = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("blocking")])),
        "balthasar": FakeBackend(review()),
        "casper": FakeBackend(review()),
    }
    assert run_headless(blocking, repo) == 1
    assert "M1" in capsys.readouterr().out

    lone_objection = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high")])),
        "balthasar": FakeBackend(review()),
        "casper": FakeBackend(review()),
    }
    assert run_headless(lone_objection, repo) == 2
    capsys.readouterr()

    assert run_headless(approve, repo / "not-a-repo") == EXIT_ERROR


def test_convene_runs_full_protocol_and_emits_events(repo):
    (repo / "a.py").write_text("x = 3\n")
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("blocking")])),
        "balthasar": FakeBackend(review()),
    }
    from magi.council import convene

    events = []
    reviews, rebuttals, merged = asyncio.run(
        convene(council, repo, "task", on_event=lambda kind, p: events.append(kind))
    )
    assert len(reviews) == 2
    assert [rb.role for rb in rebuttals] == ["balthasar"]  # melchior has nothing to answer
    assert merged["recommendation"] == "REQUEST_CHANGES"
    # progress streamed in protocol order
    assert events[0] == "packet"
    assert events.count("review") == 2
    assert events[-1] == "merged"
    assert events.index("rebuttal_start") > events.index("review")


def test_run_headless_streams_progress_to_stderr(repo, capsys):
    from magi.council import run_headless

    (repo / "a.py").write_text("x = 4\n")
    council = {r: FakeBackend(review()) for r in ("melchior", "balthasar")}
    assert run_headless(council, repo) == 0
    err = capsys.readouterr().err
    assert "MELCHIOR review: APPROVE" in err
    assert "決議 APPROVE" in err


def test_packet_uncommitted_changes(repo):
    (repo / "a.py").write_text("x = 2\n")
    packet = build_packet(repo, task="make x 2")
    assert "TASK / ACCEPTANCE CRITERIA:\nmake x 2" in packet
    assert "-x = 1" in packet and "+x = 2" in packet
    assert "working tree vs HEAD" in packet


def test_repo_branch(repo, tmp_path_factory):
    from magi.council import repo_branch

    assert repo_branch(repo) != "-"  # fresh repo has a current branch
    assert repo_branch(tmp_path_factory.mktemp("nonrepo")) == "-"


def test_packet_clean_tree_reviews_last_commit(repo):
    packet = build_packet(repo, task=None)
    assert "last commit" in packet
    assert "+x = 1" in packet
    assert "no task description provided" in packet


def test_packet_includes_untracked_files(repo):
    (repo / "new_module.py").write_text("def fresh(): return 42\n")
    packet = build_packet(repo, task="add module")
    assert "untracked files included" in packet
    assert "new_module.py" in packet
    assert "+def fresh(): return 42" in packet
    # tracked changes and untracked files appear together
    (repo / "a.py").write_text("x = 9\n")
    packet = build_packet(repo, task=None)
    assert "+x = 9" in packet and "+def fresh(): return 42" in packet


def test_packet_respects_gitignore_and_size_cap(repo):
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("token=hunter2\n")
    (repo / "huge.txt").write_text("x" * 200_000)
    packet = build_packet(repo, task=None)
    assert "hunter2" not in packet
    assert "huge.txt" in packet and "skipped" in packet
    assert "x" * 1000 not in packet


def test_packet_works_in_repo_with_no_commits(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "first.py").write_text("a = 1\n")
    packet = build_packet(tmp_path, task="first file")
    assert "+a = 1" in packet
    assert "untracked files included" in packet


# --- defects found by review ---------------------------------------------------

def test_two_members_may_pick_the_same_finding_id():
    """Members number their own findings. Two that choose one id must keep two
    findings, or a blocking finding loses its veto with no trace in the report."""
    reviews = [
        MR("melchior", "APPROVE", [finding("low", "F1")]),
        MR("balthasar", "REQUEST_CHANGES", [finding("blocking", "F1")]),
        MR("casper", "APPROVE"),
    ]
    got = merge(reviews)
    assert got["blocking_findings"] == ["B1 t"]
    assert got["recommendation"] == "REQUEST_CHANGES"


def test_member_crash_takes_one_seat_offline():
    """Any failure is that member's failure. A vendor CLI returning junk must
    not end the council: headless would exit 1, which CI reads as
    REQUEST_CHANGES."""
    class Junk:
        name, model = "junk", "j"

        async def ask(self, *a, **k):
            raise RuntimeError("envelope was a list")  # not a BackendError

    reviews = run_council(melchior=Junk(), balthasar=FakeBackend(review()))
    by_role = {r.role: r for r in reviews}
    assert by_role["melchior"].verdict == "OFFLINE"
    assert "envelope was a list" in by_role["melchior"].error
    assert by_role["balthasar"].verdict == "APPROVE"


def test_empty_packet_is_refused(tmp_path):
    """No changes and no commits reviews nothing, and nothing reads as APPROVE."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    with pytest.raises(ValueError, match="nothing to review"):
        build_packet(tmp_path)


def test_packet_budget_stops_the_untracked_scan(repo):
    """A repository that fails to ignore its build output holds thousands of
    small files. Each one clears the per-file cap; together they must not."""
    for i in range(25):
        (repo / f"blob{i}.txt").write_text("x" * 99_000)  # under _UNTRACKED_CAP
    packet = build_packet(repo)
    assert len(packet) < 2_500_000
    # a cut scope is never silent: the members must know they saw a part
    assert "truncated at" in packet


def test_run_headless_reports_an_error_as_exit_3(tmp_path, capsys):
    """Exit 1 means REQUEST_CHANGES. A failure that reviewed nothing must not
    claim it."""
    from magi.council import EXIT_ERROR, run_headless

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    council = {"melchior": FakeBackend(review())}
    assert run_headless(council, tmp_path) == EXIT_ERROR
    assert "nothing to review" in capsys.readouterr().err


def test_saved_run_keeps_the_packet_and_the_raw_replies(repo, capsys):
    """The saved run is the research record: the packet the members read, and
    each reply as the model wrote it."""
    from magi.backends import BackendError
    from magi.council import run_headless

    (repo / "a.py").write_text("x = 5\n")

    class Garbled:
        name, model = "codex", "g"

        async def ask(self, *a, **k):
            raise BackendError("expected JSON", raw="I think it is fine, actually")

    run_headless({
        "melchior": Garbled(),
        "balthasar": FakeBackend(review()),
        "casper": FakeBackend(review()),
    }, repo)
    capsys.readouterr()
    saved = json.loads((repo / ".magi" / "last-run.json").read_text())
    assert "+x = 5" in saved["packet"]
    by_role = {r["role"]: r for r in saved["reviews"]}
    assert by_role["balthasar"]["raw_text"].startswith("{")  # the reply as written
    # the reply that failed the schema is the one worth studying, so keep it whole
    assert by_role["melchior"]["raw_text"] == "I think it is fine, actually"
    assert len(list((repo / ".magi" / "runs").glob("*.json"))) == 1


def test_codex_event_labels_both_documented_and_shipped_spellings():
    """Codex documents an item's kind as `item_type` and v0.146.0 writes
    `type`; the docs say `assistant_message` where that build says
    `agent_message`. Read both, match against neither."""
    from magi.backends import _event

    assert _event('{"type":"thread.started","thread_id":"a"}') == "thread.started"
    # as the installed CLI writes it
    assert _event('{"type":"item.completed","item":{"type":"agent_message"}}') == (
        "item.completed agent_message"
    )
    # as the docs spell it, with the command the member is running
    assert _event(
        '{"type":"item.started","item":{"item_type":"command_execution","command":"rg foo"}}'
    ) == "item.started command_execution rg foo"
    assert _event('{"type":"turn.completed","usage":{"input_tokens":9}}') == (
        "turn.completed input_tokens=9"
    )
    # the stream is the only live state codex offers, so junk is still reported
    assert _event("not json") == "not json"
    assert _event("[1,2,3]") == "[1,2,3]"


def test_codex_asks_for_the_event_stream():
    """--json puts events on stdout; the answer still arrives via -o outfile."""
    cmd = CodexCli()._cmd(Path("/tmp/out.txt"), None)
    assert "--json" in cmd
    assert cmd[cmd.index("-o") + 1] == "/tmp/out.txt"


def test_codex_context_window_is_opt_in(tmp_path):
    """`codex debug models` reports gpt-5.6-sol defaulting to 272000 with a
    872000 ceiling. The council asks for the ceiling; a member on another
    model asks for nothing and keeps that model's own default."""
    from magi.council import _SOL_WINDOW, default_council

    assert _SOL_WINDOW == 872_000
    out = tmp_path / "o.txt"
    assert "model_context_window=872000" in default_council()["melchior"]._cmd(out, None)
    # unset means unset: never send a ceiling that belongs to a different model
    assert not [c for c in CodexCli()._cmd(out, None) if "model_context_window" in c]
