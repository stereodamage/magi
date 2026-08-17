"""Async wrappers around vendor CLIs (claude -p, codex exec).

Both use subscription auth — no API keys. Each backend takes a prompt
(and optional JSON schema for structured output) and returns a Reply.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class BackendError(RuntimeError):
    """A vendor CLI failed. `raw` carries whatever text it did produce.

    A reply that fails schema parsing is the most interesting one to study,
    so the council saves it instead of keeping only the truncated message.
    """

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class Reply:
    backend: str
    text: str
    data: dict | list | None  # parsed structured output when schema was given
    duration_s: float
    cost_usd: float | None = None
    raw: dict | None = None  # full vendor envelope, when available


async def _reap(proc) -> None:
    """Kill the CLI and wait for it. The wait is the point: kill() only sends
    the signal, and an unwaited child stays a zombie until magi itself exits."""
    proc.kill()
    await proc.wait()


async def _feed(proc, stdin: str) -> None:
    try:
        proc.stdin.write(stdin.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the CLI stopped reading; its exit code carries the reason
    finally:
        proc.stdin.close()


async def _pump(stream, on_line=None) -> bytes:
    """Drain a pipe to EOF, handing each line to on_line as it lands.

    Line by line, not communicate(): a line that arrives while the CLI is
    still working is the only honest sign that it is still working.
    """
    chunks = []
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            # a line longer than the 64K stream buffer. Keep the bytes and
            # drop the callback for it — progress lines are never this long.
            line = await stream.read(65536)
        if not line:
            return b"".join(chunks)
        chunks.append(line)
        if on_line is not None and (text := line.decode(errors="replace").strip()):
            on_line(text)


async def _run(
    cmd: list[str],
    stdin: str,
    timeout: float,
    cwd: Path | None,
    on_stderr=None,
    on_stdout=None,
) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as e:  # missing/unrunnable binary → member OFFLINE, not a crash
        raise BackendError(f"{cmd[0]}: cannot start: {e}") from e
    try:
        out, err, _ = await asyncio.wait_for(
            asyncio.gather(
                _pump(proc.stdout, on_stdout),
                _pump(proc.stderr, on_stderr),
                _feed(proc, stdin),
            ),
            timeout,
        )
    except TimeoutError:
        await _reap(proc)
        raise BackendError(f"{cmd[0]}: timeout after {timeout}s")
    except asyncio.CancelledError:  # operator stopped the council — do not orphan the CLI
        await _reap(proc)
        raise
    await proc.wait()  # both pipes are at EOF, so this returns at once
    if proc.returncode != 0:
        raise BackendError(
            f"{cmd[0]} exited {proc.returncode}: {err.decode()[-2000:]}",
            raw=out.decode(),
        )
    return out.decode()


def _parse_json(text: str, backend: str) -> dict | list:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise BackendError(f"{backend}: expected JSON, got: {text[:500]!r}", raw=text) from e


@dataclass
class ClaudeCli:
    """claude -p — headless Claude Code, subscription auth.

    Isolation: in -p mode any tool not in allowed_tools is denied, so the
    default tuple makes this a read-only reviewer (no Bash, no writes).
    Empty tuple = vendor defaults (no explicit grants).
    """

    model: str = "sonnet"
    effort: str | None = None  # low | medium | high | xhigh | max
    allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    pristine: bool = True  # no user/project settings, hooks, or CLAUDE.md
    name: str = "claude"

    def _cmd(self, system: str | None, schema: dict | None) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "json", "--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.pristine:
            # Empirically verified (probe + positive control, v2.1.220): with no
            # setting sources, user/project settings, hooks AND global CLAUDE.md
            # stay out of context. --bare would be stricter but kills OAuth auth.
            cmd += ["--setting-sources", ""]
        if system:
            # append (not replace): keeps built-in tool-use instructions working
            cmd += ["--append-system-prompt", system]
        if self.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        return cmd

    async def ask(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,
        timeout: float = 300.0,
        on_progress=None,
    ) -> Reply:
        t0 = asyncio.get_event_loop().time()
        out = await _run(self._cmd(system, schema), prompt, timeout, cwd,
                         on_stderr=on_progress)
        envelope = _parse_json(out, self.name)
        if envelope.get("is_error"):
            raise BackendError(f"{self.name}: {envelope.get('result', envelope)}", raw=out)
        text = envelope.get("result", "")
        data = None
        if schema:
            data = envelope.get("structured_output")
            if data is None:  # fall back to result body being the JSON itself
                data = _parse_json(text if isinstance(text, str) else json.dumps(text), self.name)
        return Reply(
            backend=self.name,
            text=text if isinstance(text, str) else json.dumps(text),
            data=data,
            duration_s=asyncio.get_event_loop().time() - t0,
            cost_usd=envelope.get("total_cost_usd"),
            raw=envelope,
        )


def _event(line: str) -> str:
    """One short label for a `codex exec --json` event.

    Documented events: thread.started, turn.started, turn.completed,
    turn.failed, item.started, item.updated, item.completed. The docs give
    an item's kind as `item_type` and v0.146.0 writes `type`, so read both;
    likewise the docs say `assistant_message` where that build says
    `agent_message`, so no name is matched against a list. turn.completed
    carries usage: input_tokens, cached_input_tokens, output_tokens.

    Codex documents no way to poll a running exec. This stream is the only
    live state there is, so a line it cannot parse is still reported.
    """
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return line[:100]
    if not isinstance(e, dict):
        return line[:100]
    parts = [str(e.get("type", "event"))]
    item = e.get("item")
    if isinstance(item, dict):
        parts.append(str(item.get("item_type") or item.get("type") or ""))
        parts.append(str(item.get("command") or "")[:40])  # what it is running
    usage = e.get("usage")
    if isinstance(usage, dict):
        parts.append(" ".join(f"{k}={v}" for k, v in usage.items()))
    return " ".join(p for p in parts if p)[:100]


@dataclass
class CodexCli:
    """codex exec — headless Codex, ChatGPT subscription auth.

    Isolation: OS-enforced (Seatbelt on macOS). read-only lets the agent run
    commands but blocks all writes; workspace-write confines writes to cwd
    and blocks network — use with a per-member worktree for experiment mode.
    """

    model: str | None = None  # None = user's configured default
    effort: str | None = None  # minimal | low | medium | high | xhigh
    service_tier: str | None = None  # "priority" = the "Fast" tier (1.5x speed, more usage)
    # None leaves codex on the model's default. `codex debug models` reports
    # both numbers per model: gpt-5.6-sol defaults to 272000 and allows up to
    # 872000, so the default throws away two thirds of the window a large diff
    # needs. Raising it past max_context_window is a 400 from the API, so the
    # value belongs beside the model that permits it, never hardcoded here.
    context_window: int | None = None
    sandbox: str = "read-only"  # read-only | workspace-write | danger-full-access
    pristine: bool = True  # skip AGENTS.md project docs
    name: str = "codex"

    def _cmd(self, outfile: Path, schemafile: Path | None) -> list[str]:
        cmd = [
            "codex", "exec", "-",  # "-" = prompt from stdin
            "--skip-git-repo-check",
            "--color", "never",
            "--ephemeral",
            "--json",  # JSONL events on stdout; the answer still goes to -o
            "--sandbox", self.sandbox,
            "-o", str(outfile),
        ]
        if self.pristine:
            cmd += ["-c", "project_doc_max_bytes=0"]
        if self.model:
            cmd += ["-m", self.model]
        if self.effort:
            cmd += ["-c", f"model_reasoning_effort={self.effort}"]
        if self.context_window:
            cmd += ["-c", f"model_context_window={self.context_window}"]
        if self.service_tier:
            # A tier the model does not advertise is only a warning, not an error:
            # codex drops it and the run proceeds at the normal tier.
            cmd += ["-c", f'service_tier="{self.service_tier}"']
        if schemafile:
            cmd += ["--output-schema", str(schemafile)]
        return cmd

    async def ask(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,
        timeout: float = 300.0,
        on_progress=None,
    ) -> Reply:
        t0 = asyncio.get_event_loop().time()
        if system:  # codex has no system-prompt flag; prepend
            prompt = f"{system}\n\n{prompt}"
        with tempfile.TemporaryDirectory(prefix="magi-codex-") as tmp:
            outfile = Path(tmp) / "last-message.txt"
            schemafile = None
            if schema:
                schemafile = Path(tmp) / "schema.json"
                schemafile.write_text(json.dumps(schema))
            await _run(
                self._cmd(outfile, schemafile), prompt, timeout, cwd,
                on_stdout=(lambda line: on_progress(_event(line))) if on_progress else None,
            )
            if not outfile.exists():
                raise BackendError(f"{self.name}: no output message written")
            text = outfile.read_text().strip()
        data = _parse_json(text, self.name) if schema else None
        return Reply(
            backend=self.name,
            text=text,
            data=data,
            duration_s=asyncio.get_event_loop().time() - t0,
        )


@dataclass
class GeminiCli:
    """gemini CLI — Google account auth. Stub: backend not implemented yet.

    Council treats a BackendError as MEMBER OFFLINE → verdict ABSTAIN.
    """

    model: str | None = None
    effort: str | None = None
    name: str = "gemini"

    async def ask(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,
        timeout: float = 300.0,
        on_progress=None,
    ) -> Reply:
        raise BackendError("gemini: backend not implemented (CLI not installed)")


# --- smoke test -------------------------------------------------------------

_SMOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocking", "high", "medium", "low"]},
                    "trigger": {"type": "string"},
                },
                "required": ["title", "severity", "trigger"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}

_SMOKE_PROMPT = """\
Review this Python function for correctness only. Report concrete findings.

def avg(xs):
    return sum(xs) / len(xs)
"""


async def _smoke() -> None:
    backends = [ClaudeCli(), CodexCli()]

    async def call(b):
        try:
            return await b.ask(_SMOKE_PROMPT, schema=_SMOKE_SCHEMA, timeout=180)
        except BackendError as e:
            return e

    for r in await asyncio.gather(*(call(b) for b in backends)):
        if isinstance(r, BackendError):
            print(f"FAIL: {r}")
            continue
        cost = f"${r.cost_usd:.4f}" if r.cost_usd else "n/a"
        print(f"OK {r.backend}: {r.duration_s:.1f}s cost={cost}")
        print(json.dumps(r.data, indent=2))


if __name__ == "__main__":
    asyncio.run(_smoke())
