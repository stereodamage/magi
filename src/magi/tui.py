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

import time
from pathlib import Path

from rich.rule import Rule
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.selection import Selection
from textual.widgets import Input, RichLog, Static

from .council import (
    Finding,
    MemberRebuttal,
    MemberReview,
    build_packet,
    convene,
    default_council,
    finding_authors,
    is_git_repo,
    repo_branch,
    save_run,
)

# the reference screen: plain bold caps, tight, with a katakana middle dot
TITLES = {
    "melchior": "MELCHIOR・1",
    "balthasar": "BALTHASAR・2",
    "casper": "CASPER・3",
}

# ponytail: 2-line half-block font, hand-drawn — 4 letters don't need pyfiglet
_MAGI_BIG = (
    "█▀▄▀█  ▄▀█  █▀▀  █\n"
    "█ ▀ █  █▀█  █▄█  █"
)

VERDICTS = {
    "APPROVE": ("可 決 / APPROVED", "approve"),
    "REQUEST_CHANGES": ("否 決 / REJECTED", "reject"),
    "ABSTAIN": ("保 留 / ABSTAIN", "abstain"),
    "OFFLINE": ("沈 黙 / SILENT", "offline"),
}

# what a member is doing right now, from its CLI's own event stream. Keys are
# matched as substrings of the event label, so a vendor that renames one leaves
# the cell on its phase label rather than showing a wrong verb.
_ACTIVITY = {
    "command_execution": "実 行 / RUNNING",
    "web_search": "検 索 / SEARCHING",
    "file_change": "編 集 / EDITING",
    "mcp_tool_call": "呼 出 / TOOL CALL",
    "reasoning": "思 考 / THINKING",
    "assistant_message": "報 告 / REPORTING",
    "agent_message": "報 告 / REPORTING",
    "turn.completed": "完 了 / COMPLETE",
    "turn.failed": "失 敗 / FAILED",
    "thread.started": "起 動 / STARTING",
}

_WAVE = "░▒▓█▓▒░ "
_PHASE_LABEL = {
    "review": "審 議 中 / DELIBERATING",
    "rebuttal": "反 論 中 / REBUTTAL",
}
_POSITION_MARKUP = {
    "ACCEPT": "[#46b87c]ACCEPT[/]",
    "PARTIALLY_ACCEPT": "[#d4b546]PARTIALLY_ACCEPT[/]",
    "CHALLENGE": "[bold #d4655a]CHALLENGE[/]",
    "OUT_OF_SCOPE": "[dim]OUT_OF_SCOPE[/]",
}
_SEV_MARKUP = {
    "blocking": "[bold #f2e9dc on #9e352c] BLOCKING [/]",
    "high": "[bold #d4655a]high[/]",
    "medium": "[#d4b546]medium[/]",
    "low": "[dim]low[/]",
}
# same hues as the member slabs, brighter: text on dark needs more luminance
# than a filled panel, or the ticker becomes the dimmest thing on screen
_VERDICT_COLOR = {
    "APPROVE": "#46b87c",
    "REQUEST_CHANGES": "#d4655a",
    "ABSTAIN": "#d4b546",
    "OFFLINE": "#5a5a5a",
    "HUMAN_REVIEW": "#d4b546",
}


def _vote(verdict: str) -> str:
    return f"[bold {_VERDICT_COLOR.get(verdict, '#cf9440')}]{verdict}[/]"


class Ticker(RichLog):
    """RichLog the mouse can select.

    Textual pulls a selection out of the widget's render, and takes it only
    when that render is Text or Content (widget.py, Widget.get_selection).
    RichLog renders a RichVisual, so the base class finds nothing and the
    findings on screen cannot be copied. The strips already carry the text.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return selection.extract("\n".join(s.text for s in self.lines)), "\n"


class MagiApp(App):
    TITLE = "MAGI SYSTEM"

    # priority: beats the focused Input's own bindings. ctrl+q still quits.
    BINDINGS = [
        Binding("ctrl+c", "stop_council", "stop the council",
                priority=True, show=False),
        Binding("y", "yank", "copy the selection", priority=True, show=False),
    ]

    CSS = """
    Screen {
        background: #0a0a06;
        color: #cf9440;
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
        border-top: double #2e8f5c;
        border-bottom: double #2e8f5c;
        color: #cc7418;
        text-style: bold;
        content-align: center middle;
        text-align: center;
    }
    #meta {
        padding: 1 0 0 1;
        color: #cf9440;
        height: 1fr;
    }
    #info {
        background: #b9d2dc;
        color: #14435c;
        text-style: bold;
        width: 16;
        height: 3;
        margin: 1 1 0 0;
        content-align: center middle;
        text-align: center;
    }
    #rightcol { align-horizontal: right; }
    #magi {
        color: #cc7418;
        text-style: bold;
        content-align: center middle;
        text-align: center;
    }
    /* canonical: solid sky-blue slabs, dark text, no border. The diagonal
       panel cuts from the show can't happen in rectangular cells. */
    .member {
        background: #58a5c4;
        color: #0c1d26;
        text-style: bold;
        height: 100%;
        padding: 0 1;
    }
    /* body takes the slack so the model line sits on the panel floor.
       Neither child sets `color`: both inherit the verdict color from .member. */
    .mbody {
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    .mmodel {
        height: 1;
        text-align: center;
        text-style: dim;
    }
    #magi { height: 100%; }
    .member.standby { background: #40758d; color: #0c1d26; }
    .member.approve  { background: #3fa06d; color: #06200f; }
    .member.reject   { background: #c04e44; color: #200604; }
    .member.abstain  { background: #cbb04e; color: #201a05; }
    .member.offline  { background: #1c1c1c; color: #5a5a5a; }
    #log {
        height: 2fr;
        min-height: 4;
        border: heavy #a86a28;
        background: #0a0a06;
        color: #cf9440;
    }
    #qbar { height: 3; }
    #qlabel {
        width: auto;
        padding: 1 1 0 2;
        color: #cc7418;
        text-style: bold;
    }
    #taskinput {
        width: 1fr;
        border: heavy #a86a28;
        background: #0a0a06;
        color: #cf9440;
    }
    #statusbar {
        height: 1;
        padding: 0 1;
        color: #cc7418;
        background: #171006;
    }
    #statusbar.approve { color: #46b87c; }
    #statusbar.reject  { color: #d4655a; }
    """

    def __init__(
        self,
        council: dict[str, object] | None = None,
        repo: Path = Path("."),
        task: str | None = None,
        packet: str | None = None,
        mode: str = "code",
        autostart: bool = False,
    ) -> None:
        super().__init__()
        self.council = council or default_council()
        self.repo = Path(repo).resolve()
        self.task_text = task
        self.packet = packet
        self.mode = mode
        self.autostart = autostart
        self.result: dict | None = None
        self._t0 = time.monotonic()
        self._frame = 0
        self._deliberating = False
        self._diff_lines = "----"
        self._phase = "review"
        self._pending: set[str] = set()
        self._worker = None
        self._reviews: list[MemberReview] = []
        self._sent = ""
        self._activity: dict[str, str] = {}  # role → what its CLI is doing

    # --- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="console"):
            with Grid(id="board"):
                with Vertical(id="leftcol"):
                    yield Static("質 問 / QUESTION", classes="jheader")
                    yield Static(self._meta_text(), id="meta")
                yield from self._member_panel("balthasar")
                with Vertical(id="rightcol"):
                    yield Static("解 決 / RESOLUTION", classes="jheader")
                    yield Static("情 報 / INFO", id="info")
                yield from self._member_panel("casper")
                yield Static(_MAGI_BIG, id="magi")
                yield from self._member_panel("melchior")
            yield Static(id="statusbar")
            yield Ticker(id="log", wrap=True, markup=True)
            with Horizontal(id="qbar"):
                yield Static("question:", id="qlabel")
                yield Input(
                    id="taskinput",
                    placeholder=f"task / acceptance criteria — repo: {self.repo}",
                )

    def _member_panel(self, role: str) -> ComposeResult:
        with Vertical(id=role, classes="member standby"):
            yield Static(id=f"{role}-body", classes="mbody")
            yield Static(self._model_line(role), classes="mmodel")

    def on_mount(self) -> None:
        for role in self.council:
            self._paint_standby(role)
        self.set_interval(0.25, self._tick)
        self.query_one("#taskinput", Input).focus()
        if self.task_text or self.autostart:  # explicit CLI intent: convene now
            self._convene()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.task_text = event.value.strip() or None
        self._convene()

    # --- rendering ------------------------------------------------------------

    def _log(self, line: str) -> None:
        self.query_one("#log", Ticker).write(line)

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

    def _body(self, role: str) -> Static:
        return self.query_one(f"#{role}-body", Static)

    def _paint_standby(self, role: str) -> None:
        self._body(role).update(f"[bold]{TITLES[role]}[/]\n\n—  S T A N D B Y  —")

    def _paint_running(self, role: str) -> None:
        wave = _WAVE[self._frame % len(_WAVE):] + _WAVE[: self._frame % len(_WAVE)]
        # the member's own activity when its CLI reports one, else the phase
        label = self._activity.get(role) or _PHASE_LABEL.get(
            self._phase, _PHASE_LABEL["review"]
        )
        # label above the wave: side panels are too narrow to flank it
        self._body(role).update(
            f"[bold]{TITLES[role]}[/]\n\n{label}\n{wave}{wave[::-1]}"
        )

    def _paint_verdict(self, r: MemberReview, verdict: str | None = None) -> None:
        # verdict overrides r.verdict after a rebuttal. The review keeps the
        # vote it cast; merge() carries the updated one into the resolution.
        verdict = verdict or r.verdict
        # unknown verdict falls back to the raw word, so nothing renders as "？"
        label, css = VERDICTS.get(verdict, (verdict, "abstain"))
        panel = self.query_one(f"#{r.role}", Vertical)
        panel.remove_class("approve", "reject", "abstain", "offline")
        panel.add_class(css)
        n = len(r.findings)
        detail = f"{n} finding{'s' if n != 1 else ''}" if not r.error else "no response"
        self._body(r.role).update(
            f"[bold]{TITLES[r.role]}[/]\n\n"
            f"[bold]{label}[/]\n{detail}  ·  {r.duration_s:.0f}s"
        )

    def _tick(self) -> None:
        bars = self.query("#statusbar")
        if not bars:  # the timer can outlive the screen while the app shuts down
            return
        self._frame += 1
        for role in self._pending:
            self._paint_running(role)
        bar = bars.first(Static)
        if self._deliberating:
            elapsed = time.monotonic() - self._t0
            waiting = ", ".join(sorted(self._pending)) or "—"
            clock = f"T+{elapsed:.0f}s"  # pad the whole token, not the number
            bar.update(f"{clock:<9}deliberating: {waiting}")
        elif self.result is None:
            bar.update("STANDBY — enter the question below to convene")

    # --- council --------------------------------------------------------------

    def _convene(self) -> None:
        if self._deliberating:
            return
        if self.packet is None and not is_git_repo(self.repo):
            self._log(f"[bold red]not a git repository:[/] {self.repo}")
            return
        try:  # built here, not in the worker: nothing flips to DELIBERATING
            packet = self.packet or build_packet(self.repo, self.task_text)
        except ValueError as e:
            self._log(f"[bold red]{e}[/]")
            return
        self._diff_lines = str(
            sum(1 for line in packet.splitlines() if line.startswith(("+", "-")))
        )
        self.result = None
        self._deliberating = True
        self._phase = "review"
        self._t0 = time.monotonic()
        self._pending = set(self.council)
        self._activity.clear()
        bar = self.query_one("#statusbar", Static)
        bar.remove_class("approve", "reject")
        self.query_one("#taskinput", Input).disabled = True  # no edits mid-session
        for role in self.council:
            panel = self.query_one(f"#{role}", Vertical)
            panel.remove_class("standby", "approve", "reject", "abstain", "offline")
            self._paint_running(role)
        self._refresh_meta()
        self._log("[bold]Council convened.[/]")
        self._log(
            f"Q: {self.task_text or '[dim]no task text — repo changes reviewed as-is[/]'}"
        )
        self.query_one("#log", Ticker).write(Rule(style="#5a4a20"))
        self._worker = self.run_worker(self._deliberate(packet), exclusive=True)

    def action_yank(self) -> None:
        """vim's y: copy the mouse selection.

        With nothing selected the key is not ours — SkipAction hands it back,
        so `y` still types a `y` in the question field.
        """
        text = self.screen.get_selected_text()
        if not text:
            raise SkipAction()
        self.copy_to_clipboard(text)
        self._log(f"[dim]yanked {len(text)} characters[/]")

    def action_stop_council(self) -> None:
        """ctrl+c — abandon the session at any stage and return to standby."""
        if not self._deliberating:
            return self.action_help_quit()  # idle: keep the "press ctrl+q" hint
        if self._worker is not None:
            # convene() cancels the members still in flight on its way out,
            # and cancelling a member kills its CLI process
            self._worker.cancel()
        self._log("[bold red]中 止 / ABORTED[/] — council stopped by the operator")
        self._deliberating = False
        self._pending.clear()
        self._refresh_meta()
        for role in self.council:
            panel = self.query_one(f"#{role}", Vertical)
            panel.remove_class("approve", "reject", "abstain", "offline")
            panel.add_class("standby")
            self._paint_standby(role)
        self.query_one("#statusbar", Static).remove_class("approve", "reject")
        inp = self.query_one("#taskinput", Input)
        inp.disabled = False
        inp.focus()

    async def _deliberate(self, packet: str) -> None:
        self._reviews = []
        self._sent = packet  # saved with the run: the prompt beside the replies
        self._refresh_meta()
        reviews, rebuttals, merged = await convene(
            self.council, self.repo, packet=packet, mode=self.mode,
            on_event=self._on_event,
        )
        self._finish(merged, reviews, rebuttals)

    def _on_event(self, kind: str, payload) -> None:
        """Paint one protocol event. convene() runs the protocol; the TUI only
        draws it, so both front ends share a single implementation."""
        if kind == "review":
            self._reviews.append(payload)
            self._show_review(payload)
        elif kind == "rebuttal_start":
            self._start_rebuttal(payload)
        elif kind == "rebuttal":
            self._show_rebuttal(payload)
        elif kind == "progress":
            role, line = payload
            for key, verb in _ACTIVITY.items():
                if key in line:
                    self._activity[role] = verb
                    break

    def _show_review(self, r: MemberReview) -> None:
        self._pending.discard(r.role)
        self._paint_verdict(r)
        n = len(r.findings)
        self._log(
            f"[dim][{r.role.upper()}][/] {_vote(r.verdict)}"
            f" [dim]· {n} finding{'s' if n != 1 else ''}"
            f" · {r.duration_s:.0f}s[/]"
        )
        if r.error:
            self._log(f"[dim][{r.role.upper()}] offline: {r.error[:120]}[/]")
        for raw in r.findings:
            f = Finding.of(raw)
            sev = _SEV_MARKUP.get(f.severity, f.severity)
            loc = f" [dim]{f.location}[/]" if f.location else ""
            self._log(
                f"[bold]{f.id}:[/] {f.title}  {sev}"
                f" [dim](conf {f.confidence:.2f})[/]{loc}"
            )

    def _start_rebuttal(self, roles: list[str]) -> None:
        self._phase = "rebuttal"
        self._pending = set(roles)
        self.query_one("#log", Ticker).write(
            Rule("反 論 / REBUTTAL — findings cross-examined", style="#5a4a20")
        )
        for role in roles:
            panel = self.query_one(f"#{role}", Vertical)
            panel.remove_class("approve", "reject", "abstain", "offline")

    def _show_rebuttal(self, rb: MemberRebuttal) -> None:
        self._pending.discard(rb.role)
        review = next(r for r in self._reviews if r.role == rb.role)
        verdict = review.verdict
        if rb.error:
            self._log(f"[dim][{rb.role.upper()}] rebuttal failed: {rb.error[:120]}[/]")
        else:
            author = finding_authors(self._reviews)  # a position targets someone else's finding
            for resp in rb.responses:
                pos = _POSITION_MARKUP.get(resp["position"], resp["position"])
                reason = resp.get("reason", "")  # RichLog wraps it; do not cut
                fid = resp["finding_id"]
                filed_by = author.get(fid)
                who = rb.role.upper()
                if filed_by and filed_by != rb.role:
                    who = f"{who} → {filed_by.upper()}"
                self._log(f"[dim][{who}][/] {pos} [bold]{fid}[/] [dim]{reason}[/]")
            if rb.updated_verdict not in ("", "UNCHANGED") and rb.updated_verdict != review.verdict:
                self._log(
                    f"[dim][{rb.role.upper()}][/] verdict updated:"
                    f" {_vote(review.verdict)} → {_vote(rb.updated_verdict)}"
                )
                verdict = rb.updated_verdict  # painted only — the review keeps its vote
        self._paint_verdict(review, verdict)

    def _write_run(
        self, merged: dict, reviews: list[MemberReview], rebuttals: list[MemberRebuttal]
    ) -> None:
        """Persist the full deliberation — the ticker only shows a summary."""
        import os

        try:
            path = save_run(self.repo, reviews, rebuttals, merged, self._sent)
        except OSError as e:
            self._log(f"[dim]could not save the run: {e}[/]")
            return
        self._log(f"[dim]full run: {os.path.relpath(path)}[/]")  # absolute path wraps

    def _finish(
        self,
        merged: dict,
        reviews: list[MemberReview],
        rebuttals: list[MemberRebuttal],
    ) -> None:
        self.result = merged
        self._deliberating = False
        self._refresh_meta()
        inp = self.query_one("#taskinput", Input)
        inp.disabled = False
        inp.focus()
        rec = merged["recommendation"]
        bar = self.query_one("#statusbar", Static)
        bar.add_class("approve" if rec == "APPROVE" else "reject")
        elapsed = time.monotonic() - self._t0
        bar.update(f"決 議 / RESOLUTION — {rec}   (T+{elapsed:.0f}s)")
        if merged["blocking_findings"]:
            self._log("[bold red]blocking:[/] " + "; ".join(merged["blocking_findings"]))
        if merged.get("disputed_findings"):
            self._log("[yellow]disputed (human decision):[/] " + "; ".join(merged["disputed_findings"]))
        votes = "  ".join(
            f"[dim]{role}:[/]{_vote(v)}" for role, v in merged["votes"].items()
        )
        self._log(f"{_vote(rec)} 決 議 / resolution  ·  {votes}")
        self._write_run(merged, reviews, rebuttals)  # last: the path stays on screen


def _init_main(argv: list[str]) -> None:
    import argparse

    from .config import detect_clis, global_config_path, init

    ap = argparse.ArgumentParser(
        prog="magi init", description="detect installed CLIs and write a council config"
    )
    ap.add_argument("--local", action="store_true",
                    help="write ./magi.toml instead of the global config")
    ap.add_argument("--force", action="store_true", help="overwrite an existing config")
    args = ap.parse_args(argv)
    dest = Path("magi.toml") if args.local else global_config_path()
    detected = detect_clis()
    print(f"detected CLIs: {', '.join(sorted(detected)) or 'none'}")
    try:
        text = init(dest, detected=detected, force=args.force)
    except (FileExistsError, ValueError) as e:
        raise SystemExit(f"magi init: {e}")
    print(f"wrote {dest}:\n\n{text}")


def _plan_main(argv: list[str]) -> None:
    import argparse
    import sys

    from .config import load_council
    from .council import EXIT_ERROR, build_plan_packet, run_headless

    ap = argparse.ArgumentParser(
        prog="magi plan",
        description="review a plan / design / idea document before implementation",
    )
    ap.add_argument("document", help="markdown (or text) proposal document")
    ap.add_argument("context", nargs="*", help="goals / constraints for the proposal")
    ap.add_argument("--report", action="store_true", help="headless text report")
    ap.add_argument("--json", action="store_true", help="headless JSON result")
    args = ap.parse_args(argv)
    doc = Path(args.document).resolve()
    if not doc.is_file():
        print(f"magi plan: no such document: {doc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    context = " ".join(args.context) or None
    try:
        council = load_council(doc.parent)
    except ValueError as e:
        print(f"magi: config error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    packet = build_plan_packet(doc, context)
    if args.json or args.report or not sys.stdout.isatty():
        raise SystemExit(run_headless(
            council, doc.parent, context, as_json=args.json, packet=packet, mode="plan",
        ))
    MagiApp(
        council=council, repo=doc.parent, task=context,
        packet=packet, mode="plan", autostart=True,
    ).run()


def _pr_main(argv: list[str]) -> None:
    import argparse
    import sys

    from .config import load_council
    from .council import EXIT_ERROR, build_pr_packet, run_headless

    ap = argparse.ArgumentParser(
        prog="magi pr",
        description="review a GitHub pull request via gh — the working tree stays untouched",
    )
    ap.add_argument("number", nargs="?", type=int,
                    help="PR number (default: the current branch's PR)")
    ap.add_argument("task", nargs="*", help="extra task / acceptance criteria")
    ap.add_argument("--report", action="store_true", help="headless text report")
    ap.add_argument("--json", action="store_true", help="headless JSON result")
    args = ap.parse_args(argv)
    repo = Path.cwd()
    task = " ".join(args.task) or None
    try:
        council = load_council(repo)
    except ValueError as e:
        print(f"magi: config error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    try:
        packet = build_pr_packet(repo, args.number, task)
    except ValueError as e:
        print(f"magi pr: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    if args.json or args.report or not sys.stdout.isatty():
        raise SystemExit(run_headless(
            council, repo, task, as_json=args.json, packet=packet,
        ))
    MagiApp(
        council=council, repo=repo, task=task, packet=packet, autostart=True,
    ).run()


def _epilog(cwd: Path) -> str:
    from .config import describe, help_theme

    t = help_theme()
    h, lit, r = t.heading, t.long_option, t.reset
    council, _, table = describe(cwd).partition("\n")  # colour its heading too
    return (
        f"{h}commands:{r}\n"
        f"  {lit}magi [repo] [task]{r}     review the working tree, or the last commit\n"
        f"                         when the tree is clean\n"
        f"  {lit}magi plan DOC [goals]{r}  review a plan or design before you build it\n"
        f"  {lit}magi pr [NUMBER]{r}       review a GitHub PR via gh (default: the\n"
        f"                         current branch's PR)\n"
        f"  {lit}magi init [--local]{r}    detect installed CLIs, write a council config\n"
        "\n"
        f"{h}exit codes (headless):{r}\n"
        f"  {t.action}0{r} APPROVE · {t.action}1{r} REQUEST_CHANGES ·"
        f" {t.action}2{r} HUMAN_REVIEW · {t.action}3{r} error\n"
        "\n"
        f"{h}{council}{r}\n{table}\n"
        "\n"
        "Every run is saved to .magi/runs/ with the packet and the raw replies."
    )


def main() -> None:
    import argparse
    import sys

    from .config import load_council

    if sys.argv[1:2] == ["init"]:
        return _init_main(sys.argv[2:])
    if sys.argv[1:2] == ["plan"]:
        return _plan_main(sys.argv[2:])
    if sys.argv[1:2] == ["pr"]:
        return _pr_main(sys.argv[2:])

    ap = argparse.ArgumentParser(
        prog="magi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "MAGI review council. Three model personas read your change on their\n"
            "own, then cross-examine each other's findings. A blocking finding\n"
            "that survives the cross-examination vetoes approval."
        ),
        epilog=_epilog(Path.cwd()),
    )
    ap.add_argument("repo", nargs="?", default=".", help="repository to review")
    ap.add_argument("task", nargs="*", help="task / acceptance criteria")
    ap.add_argument("--report", action="store_true",
                    help="headless: text report on stdout, exit code carries the verdict")
    ap.add_argument("--json", action="store_true",
                    help="headless: JSON result on stdout, exit code carries the verdict")
    if sys.argv[1:2] == ["help"]:  # before this, `magi help` reviewed ./help
        return ap.print_help()
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    task = " ".join(args.task) or None
    try:
        council = load_council(repo)
    except ValueError as e:
        from .council import EXIT_ERROR

        print(f"magi: config error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)  # exit 1 would read as REQUEST_CHANGES in CI
    if args.json or args.report or not sys.stdout.isatty():
        from .council import run_headless

        raise SystemExit(run_headless(council, repo, task, as_json=args.json))
    MagiApp(council=council, repo=repo, task=task).run()


if __name__ == "__main__":
    main()
