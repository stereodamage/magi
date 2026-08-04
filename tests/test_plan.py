"""Plan mode and prompt-file loading — synthetic, no live calls."""

import asyncio
import json

import pytest
from conftest import FakeBackend, finding, review

from magi.council import build_plan_packet, merge, run_headless
from magi.personas import PERSONAS, PLAN_PREAMBLE, PROTOCOL, REBUTTAL_PROTOCOL, system_prompt


# --- prompts live in files ------------------------------------------------------

def test_prompts_load_from_files():
    from magi import personas
    from pathlib import Path

    prompts = Path(personas.__file__).parent / "prompts"
    assert {p.stem for p in prompts.glob("*.md")} >= {
        "melchior", "balthasar", "casper", "protocol", "rebuttal", "plan",
    }
    assert "MELCHIOR-1" in PERSONAS["melchior"]
    assert "COUNCIL PROTOCOL" in PROTOCOL
    assert "REBUTTAL PROTOCOL" in REBUTTAL_PROTOCOL
    assert "PLAN REVIEW MODE" in PLAN_PREAMBLE


def test_system_prompt_composition():
    review_code = system_prompt("melchior")
    assert review_code.startswith(PERSONAS["melchior"])
    assert PROTOCOL in review_code and PLAN_PREAMBLE not in review_code

    review_plan = system_prompt("casper", "review", "plan")
    assert PLAN_PREAMBLE in review_plan and PROTOCOL in review_plan
    assert review_plan.index(PERSONAS["casper"]) < review_plan.index(PLAN_PREAMBLE)

    rebuttal_plan = system_prompt("balthasar", "rebuttal", "plan")
    assert REBUTTAL_PROTOCOL in rebuttal_plan and PLAN_PREAMBLE in rebuttal_plan
    assert PROTOCOL not in rebuttal_plan.replace(REBUTTAL_PROTOCOL, "")


# --- plan packet and merge floor -------------------------------------------------

def test_build_plan_packet(tmp_path):
    doc = tmp_path / "DESIGN.md"
    doc.write_text("# The plan\nWe cache everything forever.\n")
    packet = build_plan_packet(doc, "memory must stay bounded")
    assert "PROPOSAL PACKET" in packet
    assert "DESIGN.md" in packet
    assert "cache everything forever" in packet
    assert "memory must stay bounded" in packet
    assert "questions" in build_plan_packet(doc)  # no context → ask, don't guess


def test_plan_mode_never_approves():
    from magi.council import MemberReview

    reviews = [
        MemberReview(role, "fake", "APPROVE", review("APPROVE"))
        for role in ("melchior", "balthasar", "casper")
    ]
    assert merge(reviews)["recommendation"] == "APPROVE"
    got = merge(reviews, mode="plan")
    assert got["recommendation"] == "HUMAN_REVIEW"
    assert got["mode"] == "plan"


# --- headless plan run ------------------------------------------------------------

def test_run_headless_plan_no_git_needed(tmp_path, capsys):
    doc = tmp_path / "IDEA.md"  # tmp_path is not a git repository
    doc.write_text("# Idea\nStore sessions in a global dict.\n")
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high")])),
        "balthasar": FakeBackend(review()),
        "casper": FakeBackend(review()),
    }
    packet = build_plan_packet(doc, "must survive restarts")
    code = run_headless(council, tmp_path, "must survive restarts",
                        as_json=True, packet=packet, mode="plan")
    assert code == 2  # lone objection → HUMAN_REVIEW; plan mode can never hit 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "plan"
    # members saw the plan preamble and the document
    melchior_call = council["melchior"].calls[0]
    assert "PLAN REVIEW MODE" in melchior_call["system"]
    assert "Store sessions in a global dict" in melchior_call["prompt"]


def test_plan_review_member_uses_plan_prompt(tmp_path):
    from magi.council import review_member

    backend = FakeBackend(review())
    asyncio.run(review_member("melchior", backend, "PACKET", tmp_path, mode="plan"))
    assert "PLAN REVIEW MODE" in backend.calls[0]["system"]
    asyncio.run(review_member("melchior", backend, "PACKET", tmp_path))
    assert "PLAN REVIEW MODE" not in backend.calls[1]["system"]
