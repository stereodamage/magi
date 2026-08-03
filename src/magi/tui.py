"""MAGI TUI — the council rendered the way it deserves.

Layout mirrors the canonical MAGI deliberation screen: 質問 (question) header
and system metadata on the left, 解決 (resolution) header and 情報 box on the
right, BALTHASAR-2 spanning top-center, CASPER-3 bottom-left, MELCHIOR-1
bottom-right, the MAGI label between them, and the question line at the bottom.

Panels pulse amber while a member deliberates, then flip to the verdict:
可決 (approve, green) / 否決 (request changes, red) / 保留 (abstain, yellow) /
沈黙 (offline, dark). Findings stream into the ticker as members report.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

from .council import (
    MemberReview,
    build_packet,
    default_council,
    is_git_repo,
    merge,
    repo_branch,
    review_member,
)

TITLES = {
    "melchior": "MELCHIOR • 1",
    "balthasar": "BALTHASAR • 2",
    "casper": "CASPER • 3",
}

VERDICTS = {
    "APPROVE": ("可 決", "approve"),
    "REQUEST_CHANGES": ("否 決", "reject"),
    "ABSTAIN": ("保 留", "abstain"),
    "OFFLINE": ("沈 黙", "offline"),
}

_WAVE = "░▒▓█▓▒░ "
_SEV_MARKUP = {
    "blocking": "[bold white on red] BLOCKING [/]",
    "high": "[bold red]high[/]",
    "medium": "[yellow]medium[/]",
    "low": "[dim]low[/]",
}


class MagiApp(App):
    TITLE = "MAGI SYSTEM"

    CSS = """
    Screen {
        background: #0a0a06;
        color: #ffa028;
        align: center middle;
    }
    #console {
        width: 100%;
        height: 100%;
    }
    #board {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1.2fr 1fr;
        grid-rows: 3fr 2fr;
        grid-gutter: 1 2;
        padding: 0 1;
        height: 3fr;
        min-height: 14;
    }
    #leftcol, #rightcol { height: 100%; }
    .jheader {
        height: 3;
        border-top: double #ff7b00;
        border-bottom: double #ff7b00;
        color: #ff7b00;
        text-style: bold;
        content-align: center middle;
        text-align: center;
    }
    #meta {
        padding: 1 0 0 1;
        color: #d9922f;
        height: 1fr;
    }
    #info {
        border: heavy #ffa028;
        color: #ffa028;
        text-style: bold;
        width: 12;
        height: 3;
        margin: 1 1 0 0;
        content-align: center middle;
        text-align: center;
    }
    #rightcol { align-horizontal: right; }
    #magi {
        color: #ff7b00;
        text-style: bold;
        content-align: center middle;
        text-align: center;
    }
    .member {
        border: heavy #ff7b00;
        content-align: center middle;
        text-align: center;
        color: #ffa028;
        height: 100%;
    }
    #magi { height: 100%; }
    .member.standby { border: heavy #7a4a08; color: #8a6a30; }
    .member.approve  { border: heavy #00e070; color: #00e070; background: #001a0c; }
    .member.reject   { border: heavy #ff2b2b; color: #ff4d4d; background: #1c0202; }
    .member.abstain  { border: heavy #ffd23f; color: #ffd23f; background: #1a1502; }
    .member.offline  { border: heavy #3a3a3a; color: #5a5a5a; background: #0d0d0d; }
    #log {
        height: 2fr;
        min-height: 4;
        border: heavy #ff7b00;
        background: #0a0a06;
        color: #d9922f;
    }
    #qbar { height: 3; }
    #qlabel {
        width: auto;
        padding: 1 1 0 2;
        color: #ff7b00;
        text-style: bold;
    }
    #taskinput {
        width: 1fr;
        border: heavy #ff7b00;
        background: #0a0a06;
        color: #ffa028;
    }
    #statusbar {
        height: 1;
        padding: 0 1;
        color: #ff7b00;
        background: #1a1206;
    }
    #statusbar.approve { color: #00e070; }
    #statusbar.reject  { color: #ff4d4d; }
    """

    def __init__(
        self,
        council: dict[str, object] | None = None,
        repo: Path = Path("."),
        task: str | None = None,
        packet: str | None = None,
    ) -> None:
        super().__init__()
        self.council = council or default_council()
        self.repo = Path(repo).resolve()
        self.task_text = task
        self.packet = packet
        self.result: dict | None = None
        self._t0 = time.monotonic()
        self._frame = 0
        self._deliberating = False
        self._diff_lines = "----"
        self._pending: set[str] = set()

    # --- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="console"):
            with Grid(id="board"):
                with Vertical(id="leftcol"):
                    yield Static("質  問", classes="jheader")
                    yield Static(self._meta_text(), id="meta")
                yield Static(id="balthasar", classes="member standby")
                with Vertical(id="rightcol"):
                    yield Static("解  決", classes="jheader")
                    yield Static("情 報", id="info")
                yield Static(id="casper", classes="member standby")
                yield Static("M A G I", id="magi")
                yield Static(id="melchior", classes="member standby")
            yield RichLog(id="log", wrap=True, markup=True)
            with Horizontal(id="qbar"):
                yield Static("question:", id="qlabel")
                yield Input(
                    id="taskinput",
                    placeholder=f"task / acceptance criteria — repo: {self.repo}",
                )
            yield Static(id="statusbar")

    def on_mount(self) -> None:
        for role in self.council:
            self._paint_standby(role)
        self.set_interval(0.25, self._tick)
        self.query_one("#taskinput", Input).focus()
        if self.task_text:  # explicit CLI intent: convene now
            self._convene()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.task_text = event.value.strip() or None
        self._convene()

    # --- rendering ------------------------------------------------------------

    def _log(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def _meta_text(self) -> str:
        if self._deliberating:
            status = "DELIBERATING"
        elif self.result is not None:
            status = "RESOLVED"
        else:
            status = "READY"
        return (
            f"STATUS:{status}\n"
            f"REPO:{self.repo.name or '-'}\n"
            f"BRANCH:{repo_branch(self.repo)}\n"
            f"DIFF:{self._diff_lines}"
        )

    def _refresh_meta(self) -> None:
        self.query_one("#meta", Static).update(self._meta_text())

    def _model_line(self, role: str) -> str:
        b = self.council[role]
        model = getattr(b, "model", None) or ""
        return f"{model}  ({b.name})".strip()

    def _paint_standby(self, role: str) -> None:
        self.query_one(f"#{role}", Static).update(
            f"{TITLES[role]}\n{self._model_line(role)}\n\n—  S T A N D B Y  —"
        )

    def _paint_running(self, role: str) -> None:
        wave = _WAVE[self._frame % len(_WAVE):] + _WAVE[: self._frame % len(_WAVE)]
        self.query_one(f"#{role}", Static).update(
            f"{TITLES[role]}\n{self._model_line(role)}\n\n{wave}  審 議 中  {wave[::-1]}"
        )

    def _paint_verdict(self, r: MemberReview) -> None:
        label, css = VERDICTS.get(r.verdict, ("？", "abstain"))
        panel = self.query_one(f"#{r.role}", Static)
        panel.remove_class("approve", "reject", "abstain", "offline")
        panel.add_class(css)
        n = len(r.findings)
        detail = f"{n} finding{'s' if n != 1 else ''}" if not r.error else "no response"
        panel.update(
            f"{TITLES[r.role]}\n{self._model_line(r.role)}\n\n"
            f"{label}\n{r.verdict}  ·  {detail}  ·  {r.duration_s:.0f}s"
        )

    def _tick(self) -> None:
        self._frame += 1
        for role in self._pending:
            self._paint_running(role)
        bar = self.query_one("#statusbar", Static)
        if self._deliberating:
            elapsed = time.monotonic() - self._t0
            waiting = ", ".join(sorted(self._pending)) or "—"
            bar.update(f"T+{elapsed:5.0f}s   deliberating: {waiting}")
        elif self.result is None:
            bar.update("STANDBY — enter the question below to convene")

    # --- council --------------------------------------------------------------

    def _convene(self) -> None:
        if self._deliberating:
            return
        if self.packet is None and not is_git_repo(self.repo):
            self._log(f"[bold red]not a git repository:[/] {self.repo}")
            return
        self.result = None
        self._deliberating = True
        self._t0 = time.monotonic()
        self._pending = set(self.council)
        bar = self.query_one("#statusbar", Static)
        bar.remove_class("approve", "reject")
        for role in self.council:
            panel = self.query_one(f"#{role}", Static)
            panel.remove_class("standby", "approve", "reject", "abstain", "offline")
            self._paint_running(role)
        self._refresh_meta()
        self._log(
            f"[dim]council of {len(self.council)} convened — "
            f"{self.task_text or 'no task text (reviewing repo changes as-is)'}[/]"
        )
        self.run_worker(self._deliberate(), exclusive=True)

    async def _deliberate(self) -> None:
        packet = self.packet or build_packet(self.repo, self.task_text)
        self._diff_lines = str(
            sum(1 for line in packet.splitlines() if line.startswith(("+", "-")))
        )
        self._refresh_meta()
        coros = [
            review_member(role, b, packet, self.repo) for role, b in self.council.items()
        ]
        reviews: list[MemberReview] = []
        for fut in asyncio.as_completed(coros):
            r = await fut
            reviews.append(r)
            self._pending.discard(r.role)
            self._paint_verdict(r)
            if r.error:
                self._log(f"[dim][{r.role.upper()}] offline: {r.error[:120]}[/]")
            for f in r.findings:
                sev = _SEV_MARKUP.get(f["severity"], f["severity"])
                loc = f" [dim]{f['file']}:{f['start_line']}[/]" if f.get("file") else ""
                self._log(
                    f"[bold]{f['id']}[/] {sev} {f['title']}"
                    f" [dim](conf {f['confidence']:.2f})[/]{loc}"
                )
        self._finish(merge(reviews))

    def _finish(self, merged: dict) -> None:
        self.result = merged
        self._deliberating = False
        self._refresh_meta()
        rec = merged["recommendation"]
        bar = self.query_one("#statusbar", Static)
        bar.add_class("approve" if rec == "APPROVE" else "reject")
        elapsed = time.monotonic() - self._t0
        bar.update(f"決 議 — {rec}   (T+{elapsed:.0f}s)")
        if merged["blocking_findings"]:
            self._log("[bold red]blocking:[/] " + "; ".join(merged["blocking_findings"]))
        votes = "  ".join(f"{role}:{v}" for role, v in merged["votes"].items())
        self._log(f"[bold]決議 {rec}[/]  ·  {votes}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="magi", description="MAGI review council")
    ap.add_argument("repo", nargs="?", default=".", help="repository to review")
    ap.add_argument("task", nargs="*", help="task / acceptance criteria")
    args = ap.parse_args()
    MagiApp(repo=Path(args.repo).resolve(), task=" ".join(args.task) or None).run()


if __name__ == "__main__":
    main()
