"""TUI pilot tests — fake council, no live calls, no real terminal."""

import asyncio

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
        # balthasar challenges M-001 and concedes nothing else; casper accepts it
        "balthasar": FakeBackend(
            review("APPROVE"),
            rebuttal_reply=rebuttal(responses=[position("M-001", "CHALLENGE")]),
        ),
        "casper": FakeBackend(
            review("REQUEST_CHANGES", [finding("blocking", "C-001")]),
            rebuttal_reply=rebuttal(responses=[position("M-001", "ACCEPT")]),
        ),
    }
    # melchior concedes its own verdict after seeing C-001
    council["melchior"].rebuttal_reply = rebuttal(
        "APPROVE", [position("C-001", "ACCEPT")]
    )
    app = MagiApp(council=council, packet="PACKET", task="go")
    async with app.run_test(size=(100, 32)) as pilot:
        await _wait_result(app, pilot)
        # C-001 accepted by melchior → confirmed blocking → veto
        assert app.result["recommendation"] == "REQUEST_CHANGES"
        assert app.result["blocking_findings"] == ["C-001 t"]
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
        # model line is the last row inside the border, below the body
        assert str(model.render()).startswith("m-balthasar")
        assert model.region.y == body.region.y + body.region.height
        assert model.region.bottom == panel.region.bottom - 1  # border row
        await _wait_result(app, pilot)
        assert not app.query_one("#taskinput").disabled
        # verdict color reaches the model line through the container
        assert model.rich_style.color == panel.rich_style.color


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
