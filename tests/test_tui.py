"""TUI pilot tests — fake council, no live calls, no real terminal."""

import asyncio

import pytest

from conftest import FakeBackend, finding, position, rebuttal, review

from magi.tui import MagiApp, TITLES, VERDICTS


class SlowBackend(FakeBackend):
    """Stays in flight long enough for the pilot to interact mid-deliberation."""

    async def ask(self, *a, **kw):
        await asyncio.sleep(0.4)
        return await super().ask(*a, **kw)


def fake_council():
    return {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("blocking")])),
        "balthasar": FakeBackend(review("APPROVE")),
        "casper": FakeBackend(fail=True),
    }


async def _wait_result(app, pilot, ticks=100):
    for _ in range(ticks):
        await pilot.pause(0.05)
        if app.result is not None:
            return
    raise AssertionError("council worker never finished")


async def test_bare_launch_is_standby():
    council = fake_council()
    app = MagiApp(council=council, packet=None)  # no task, no packet: standby
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.3)
        assert app.result is None
        for role in council:
            assert app.query_one(f"#{role}").has_class("standby")
        # no member was asked anything
        assert all(not b.calls for b in council.values())


async def test_input_submit_convenes():
    council = fake_council()
    app = MagiApp(council=council, packet="PACKET")  # packet override skips git check
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert app.result is None and not app._deliberating
        await pilot.click("#taskinput")
        await pilot.press(*"fix it", "enter")
        await _wait_result(app, pilot)
        assert app.task_text == "fix it"
        assert all(b.calls for b in council.values())


async def test_cli_task_convenes_and_merges():
    app = MagiApp(council=fake_council(), packet="PACKET", task="stack discounts")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
        assert app.result["recommendation"] == "REQUEST_CHANGES"
        assert app.result["offline"] == ["casper"]
        assert app.query_one("#melchior").has_class("reject")
        assert app.query_one("#balthasar").has_class("approve")
        assert app.query_one("#casper").has_class("offline")


async def test_rebuttal_updates_verdict_and_panel():
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high", "M-001")])),
        # balthasar challenges M1 and concedes nothing else; casper accepts it
        "balthasar": FakeBackend(
            review("APPROVE"),
            rebuttal_reply=rebuttal(responses=[position("M1", "CHALLENGE")]),
        ),
        "casper": FakeBackend(
            review("REQUEST_CHANGES", [finding("blocking", "C-001")]),
            rebuttal_reply=rebuttal(responses=[position("M1", "ACCEPT")]),
        ),
    }
    # melchior concedes its own verdict after seeing C1
    council["melchior"].rebuttal_reply = rebuttal(
        "APPROVE", [position("C1", "ACCEPT")]
    )
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
        # C1 accepted by melchior → confirmed blocking → veto
        assert app.result["recommendation"] == "REQUEST_CHANGES"
        assert app.result["blocking_findings"] == ["C1 t"]
        # melchior's updated verdict reflected in votes and panel
        assert app.result["votes"]["melchior"] == "APPROVE"
        assert app.query_one("#melchior").has_class("approve")
        # every member ran review + rebuttal
        assert all(len(b.calls) == 2 for b in council.values())


async def test_nonrepo_stays_standby(tmp_path):
    council = fake_council()
    # task given but repo is not a git repo and no packet override: refuse to run
    app = MagiApp(council=council, repo=tmp_path, task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.3)
        assert app.result is None and not app._deliberating
        assert all(not b.calls for b in council.values())


async def test_model_line_on_panel_floor_and_input_locked_while_deliberating():
    council = {r: SlowBackend(review("APPROVE"), model=f"m-{r}") for r in TITLES}
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.1)
        assert app.query_one("#taskinput").disabled  # no edits mid-session
        panel = app.query_one("#balthasar")
        body, model = panel.query_one(".mbody"), panel.query_one(".mmodel")
        # model line is the last row of the borderless panel, below the body
        assert str(model.render()).startswith("m-balthasar")
        assert model.region.y == body.region.y + body.region.height
        assert model.region.bottom == panel.region.bottom
        await _wait_result(app, pilot)
        assert not app.query_one("#taskinput").disabled
        # verdict color reaches the model line through the container
        assert model.rich_style.color == panel.rich_style.color


async def test_rebuttal_lines_name_the_finding_author():
    council = {
        "balthasar": FakeBackend(review("REQUEST_CHANGES", [finding("low", "B-003")])),
        "melchior": FakeBackend(
            review("APPROVE"),
            rebuttal_reply=rebuttal("REQUEST_CHANGES",
                                    [position("B1", "CHALLENGE", "no path")]),
        ),
        "casper": FakeBackend(review("APPROVE")),
    }
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(110, 32)) as pilot:
        await _wait_result(app, pilot)
        ticker = [line.text for line in app.query_one("#log").lines]
    # a position names the member it targets; a verdict change speaks for itself
    assert any("[MELCHIOR → BALTHASAR] CHALLENGE B1" in t for t in ticker)
    assert any(t.startswith("[MELCHIOR] verdict updated:") for t in ticker)


async def test_every_vote_reaches_the_ticker_in_its_verdict_color():
    from rich.color import Color

    from magi.tui import _VERDICT_COLOR

    council = {
        "melchior": FakeBackend(review("APPROVE")),
        "balthasar": FakeBackend(review("REQUEST_CHANGES", [finding("high", "B-001")])),
        "casper": FakeBackend(fail=True),  # → OFFLINE
    }
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
        seen = {}
        for line in app.query_one("#log").lines:
            for seg in line._segments:  # private, but the only view of rendered color
                if seg.text in _VERDICT_COLOR and seg.style and seg.style.color:
                    seen.setdefault(seg.text, seg.style.color.triplet)
    # every member's verdict is logged, not just the closing tally
    assert set(seen) == {"APPROVE", "REQUEST_CHANGES", "HUMAN_REVIEW"} | {"OFFLINE"}
    for verdict, triplet in seen.items():
        assert triplet == Color.parse(_VERDICT_COLOR[verdict]).triplet


async def test_finish_writes_the_full_run_to_disk(tmp_path):
    import json
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high", "M-001")])),
        "balthasar": FakeBackend(
            review("APPROVE"),
            rebuttal_reply=rebuttal(responses=[position("M1", "CHALLENGE")]),
        ),
        "casper": FakeBackend(review("APPROVE")),
    }
    app = MagiApp(council=council, repo=tmp_path, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
    run = json.loads((tmp_path / ".magi" / "last-run.json").read_text())
    assert run["recommendation"] == app.result["recommendation"]
    assert {r["role"] for r in run["reviews"]} == set(council)
    by_role = {r["role"]: r for r in run["reviews"]}
    assert by_role["melchior"]["review"]["findings"][0]["id"] == "M1"
    assert run["rebuttals"], "rebuttal round must be recorded too"
    # self-ignoring, so the log never lands in the next evidence packet
    assert (tmp_path / ".magi" / ".gitignore").read_text() == "*\n"
    ignored = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True,
    ).stdout
    assert ".magi" not in ignored


async def test_long_rebuttal_reason_wraps_instead_of_truncating():
    reason = "because " * 40  # 320 chars — far past any one ticker line
    council = {
        "melchior": FakeBackend(review("REQUEST_CHANGES", [finding("high", "M-001")])),
        "balthasar": FakeBackend(
            review("APPROVE"),
            rebuttal_reply=rebuttal(responses=[position("M1", "CHALLENGE", reason)]),
        ),
        "casper": FakeBackend(review("APPROVE")),
    }
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
        ticker = "".join(line.text for line in app.query_one("#log").lines)
        assert ticker.count("because") == 40  # every word survived the wrap


async def test_ctrl_c_stops_the_council_at_any_stage():
    for stop_in_phase in ("review", "rebuttal"):
        council = {
            "melchior": SlowBackend(review("REQUEST_CHANGES", [finding("high", "M-001")])),
            "balthasar": SlowBackend(review("APPROVE")),
            "casper": SlowBackend(review("APPROVE")),
        }
        app = MagiApp(council=council, packet="PACKET", task="go")
        async with app.run_test(size=(100, 32)) as pilot:
            if stop_in_phase == "rebuttal":
                for _ in range(60):  # let the review round land first
                    await pilot.pause(0.05)
                    if app._phase == "rebuttal":
                        break
            await pilot.pause(0.1)
            assert app._deliberating and app._phase == stop_in_phase
            await pilot.press("ctrl+c")
            assert not app._deliberating
            assert app.result is None  # no verdict is merged from a stopped council
            assert not app.query_one("#taskinput").disabled
            for role in council:
                assert app.query_one(f"#{role}").has_class("standby")
            await pilot.pause(0.6)  # the abandoned members must not repaint anything
            assert app.result is None and not app._deliberating


async def test_layout_covers_all_roles():
    app = MagiApp(council=fake_council())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        for role in ("melchior", "balthasar", "casper"):
            assert app.query_one(f"#{role}") is not None
        # canonical screen furniture: metadata, MAGI label, 情報 box, question bar
        for wid in ("#meta", "#magi", "#info", "#qbar", "#taskinput"):
            assert app.query_one(wid) is not None
        meta = str(app.query_one("#meta").render())
        assert "STATUS:READY" in meta and "BRANCH:" in meta
    assert set(TITLES) == set(fake_council())
    assert set(VERDICTS) == {"APPROVE", "REQUEST_CHANGES", "ABSTAIN", "OFFLINE"}


def test_help_verb_matches_dash_help_and_lists_every_command(tmp_path, monkeypatch, capsys):
    """`magi help` used to review a directory named `help`."""
    import sys

    from magi.tui import main

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["magi", "help"])
    main()
    text = capsys.readouterr().out
    assert "magi plan DOC" in text and "magi init" in text  # subcommands
    assert "magi pr" in text
    assert "council in use" in text  # which config is live
    assert "[repo] [task ...]" in text  # the real usage line, not `magi [-h]`
    # argparse colours its own sections; the epilog and the council table
    # must follow it, NO_COLOR included
    assert "\x1b[" not in text

    monkeypatch.setattr(sys, "argv", ["magi", "--help"])
    with pytest.raises(SystemExit):
        main()
    assert capsys.readouterr().out == text


def test_pr_verb_reviews_a_pr_headless(tmp_path, monkeypatch):
    """`magi pr 42 --report` builds the packet from gh and runs headless on it."""
    import sys

    import magi.config as config
    import magi.council as council
    from magi.tui import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "load_council", lambda repo: {"melchior": object()})
    monkeypatch.setattr(
        council, "build_pr_packet",
        lambda repo, number=None, task=None: f"PACKET {number} {task}",
    )
    seen = {}

    def fake_run_headless(c, repo, task=None, as_json=False, packet=None, **kw):
        seen.update(packet=packet, as_json=as_json)
        return 0

    monkeypatch.setattr(council, "run_headless", fake_run_headless)
    monkeypatch.setattr(sys, "argv", ["magi", "pr", "42", "--report"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert seen["packet"] == "PACKET 42 None"


def test_pr_verb_reports_gh_errors(tmp_path, monkeypatch, capsys):
    import sys

    import magi.config as config
    import magi.council as council
    from magi.tui import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "load_council", lambda repo: {"melchior": object()})

    def boom(repo, number=None, task=None):
        raise ValueError("no pull requests found")

    monkeypatch.setattr(council, "build_pr_packet", boom)
    monkeypatch.setattr(sys, "argv", ["magi", "pr", "--report"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == council.EXIT_ERROR
    assert "no pull requests found" in capsys.readouterr().err


async def test_activity_shows_in_the_cell_and_the_status_line_divides():
    """A member reports what it is doing in its own cell, not truncated into
    a bar at the bottom. The status line divides the board from the ticker."""
    from magi.backends import Reply
    from textual.widgets import Static

    class Reporting(FakeBackend):
        def __init__(self, event):
            super().__init__(review("APPROVE"))
            self.event = event

        async def ask(self, prompt, **kw):
            if kw.get("on_progress"):
                kw["on_progress"](self.event)
            await asyncio.sleep(0.6)
            return Reply(self.name, "{}", self.review, 0.6)

    app = MagiApp(
        council={
            "melchior": Reporting("item.started command_execution rg foo"),
            "balthasar": Reporting("item.started reasoning"),
            "casper": Reporting("turn.failed"),
        },
        packet="PACKET", task="go",
    )
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.3)
        board, bar, log = (app.query_one(s) for s in ("#board", "#statusbar", "#log"))
        assert board.region.bottom <= bar.region.y < log.region.y
        cells = {r: str(app.query_one(f"#{r}-body", Static).render()) for r in
                 ("melchior", "balthasar", "casper")}
        assert "RUNNING" in cells["melchior"]
        assert "THINKING" in cells["balthasar"]
        assert "FAILED" in cells["casper"]
        # an event nobody maps leaves the phase label in place
        app._activity.clear()
        app._paint_running("melchior")
        assert "DELIBERATING" in str(app.query_one("#melchior-body", Static).render())
