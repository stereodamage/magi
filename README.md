<h1 align="center">M A G I</h1>

<p align="center">
  <em>Evangelion-style deliberation council for code review.</em>
</p>

<p align="center">
  Three AI personas — <strong>MELCHIOR·1</strong> (correctness), <strong>BALTHASAR·2</strong> (stewardship),
  <strong>CASPER·3</strong> (intent &amp; design) —<br>
  independently review your diff and vote. 可決 / 否決.
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12+-ff7b00">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-ff7b00">
  <img alt="No API keys" src="https://img.shields.io/badge/API%20keys-none%20required-ff7b00">
</p>

<p align="center">
  <img alt="MAGI verdict screen — council resolved, findings in the ticker" src="docs/verdict.svg" width="920">
</p>

Runs on the LLM subscriptions you already pay for — members are vendor CLIs
spawned headless, so a Claude Pro/Max/Team seat and a ChatGPT plan are the
only requirements.

---

## Why three reviewers

One model reviewing code produces one generic list of bugs. MAGI forces three
genuinely different reviews:

| Member | Mandate | Central question |
|---|---|---|
| **MELCHIOR·1** | Correctness & evidence | *Show me the execution path on which this fails.* |
| **BALTHASAR·2** | Stewardship & risk | *If this goes wrong at 3 a.m., who pays for it — can they recover?* |
| **CASPER·3** | Intent & design | *Is this the right change, expressed the right way?* |

Each member reviews privately against the same evidence packet — task
description, diff, read-only access to the repository — and returns
schema-enforced findings: every finding must name a trigger, an observable
failure, and evidence. Vague output ("consider refactoring", "add more
tests") is banned by protocol.

Once the initial reviews lock, the **rebuttal round** (反論) convenes: each
member responds to the other members' findings at the finding level —
`ACCEPT / PARTIALLY_ACCEPT / CHALLENGE / OUT_OF_SCOPE` — where challenges
require evidence, and may update its own verdict after reading the others.

Verdicts then merge under **asymmetric rules**:

- a confirmed blocking finding vetoes approval, regardless of votes;
- a finding challenged by every responder and supported by none is
  **disputed** — it stops vetoing, but a disputed *blocking* finding
  escalates to the human instead of allowing approval;
- non-blocking objections need two members — or a human — to block;
- a member erroring out goes 沈黙 (offline) and abstains: the council
  degrades instead of failing.

---

## How it works

| Backend | Auth | Isolation |
|---|---|---|
| `claude -p` | Claude subscription (OAuth) | pristine context (`--setting-sources ""` — no user settings, hooks, or CLAUDE.md reach the council); tools restricted to `Read` / `Grep` / `Glob` |
| `codex exec` | ChatGPT subscription | `--ephemeral`, project docs off, OS-enforced `--sandbox read-only` (Seatbelt / Landlock) |
| `gemini` | — | stub for a third model family (planned) |

Structured output is enforced end-to-end: `--json-schema` (claude) and
`--output-schema` (codex) guarantee every review parses.

The default council seats **MELCHIOR on `gpt-5.6-sol`**, **BALTHASAR on
`claude-opus-5`**, and **CASPER on `claude-fable-5`** — correctness
deliberately sits on a different model family than the members judging it.
Edit `default_council()` in `src/magi/council.py` to taste.

---

## Usage

```sh
uv run magi                       # standby: TUI opens, nothing runs
uv run magi /path/to/repo         # standby with explicit repo
uv run magi . "task description"  # convenes immediately
```

Bare launch is always **standby** — the council convenes only when you type
the question (task / acceptance criteria) and press enter. MAGI reviews
uncommitted changes against `HEAD`, falling back to the last commit on a
clean tree.

While the council sits, the TUI mirrors the canonical MAGI deliberation
screen: the 質問 block tracks repo, branch, diff size, and status; the three
panels pulse 審議中; verdicts flip panels to 可決 (green) / 否決 (red) /
保留 (yellow) / 沈黙 (dark); findings stream into the ticker; and the merged
決議 lands in the status bar with blocking findings called out.

---

## Development

```sh
uv sync
uv run pytest   # synthetic tests only — fake backends, no live model calls
```

## Roadmap

- **Gemini backend** for the third seat
- **Council config file** — per-member model/effort without touching code
- **Experiment mode** — per-member git worktrees with `--sandbox workspace-write`
- **Non-TTY mode** — plain report + exit code for CI

---

## License

[MIT](LICENSE).

Fan homage — not affiliated with khara, Gainax, or the Evangelion franchise.
