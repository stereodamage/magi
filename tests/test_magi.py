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
    assert got["blocking_findings"] == ["M-001 t"]


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
    # melchior's second call: sees B-001 as target, own M-001 only as reference
    rebut_call = m.calls[1]
    assert rebut_call["schema"] is REBUTTAL_SCHEMA
    assert "FINDINGS TO RESPOND TO" in rebut_call["prompt"]
    assert '"B-001"' in rebut_call["prompt"].split("FINDINGS TO RESPOND TO")[1]
    assert "YOUR ORIGINAL REVIEW" in rebut_call["prompt"]
    assert REBUTTAL_PROTOCOL in rebut_call["system"]


def test_disputed_blocking_escalates_to_human():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M-001", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M-001", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["recommendation"] == "HUMAN_REVIEW"
    assert got["blocking_findings"] == []
    assert got["disputed_findings"] == ["M-001 t"]


def test_supported_blocking_still_vetoes_despite_challenge():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M-001", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M-001", "PARTIALLY_ACCEPT")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["recommendation"] == "REQUEST_CHANGES"
    assert got["blocking_findings"] == ["M-001 t"]
    assert got["disputed_findings"] == []


def test_author_cannot_support_own_finding():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
        MR("casper", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("melchior", "fake", "UNCHANGED", [position("M-001", "ACCEPT")]),
        MemberRebuttal("balthasar", "fake", "UNCHANGED", [position("M-001", "CHALLENGE")]),
        MemberRebuttal("casper", "fake", "UNCHANGED", [position("M-001", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["disputed_findings"] == ["M-001 t"]
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


def test_failed_rebuttal_never_weakens_findings():
    reviews = [
        MR("melchior", "REQUEST_CHANGES", [finding("blocking")]),
        MR("balthasar", "APPROVE"),
    ]
    rebuttals = [
        MemberRebuttal("balthasar", "fake", error="timeout",
                       responses=[position("M-001", "CHALLENGE")]),
    ]
    got = merge(reviews, rebuttals)
    assert got["blocking_findings"] == ["M-001 t"]
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
    assert "M-001" in capsys.readouterr().out

    lone_objection = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high")])),
        "balthasar": FakeBackend(review()),
        "casper": FakeBackend(review()),
    }
    assert run_headless(lone_objection, repo) == 2
    capsys.readouterr()

    assert run_headless(approve, repo / "not-a-repo") == EXIT_ERROR


def test_convene_runs_full_protocol(repo):
    (repo / "a.py").write_text("x = 3\n")
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("blocking")])),
        "balthasar": FakeBackend(review()),
    }
    from magi.council import convene

    reviews, rebuttals, merged = asyncio.run(convene(council, repo, "task"))
    assert len(reviews) == 2
    assert [rb.role for rb in rebuttals] == ["balthasar"]  # melchior has nothing to answer
    assert merged["recommendation"] == "REQUEST_CHANGES"


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
