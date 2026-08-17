"""Council configuration: magi.toml loading and `magi init` scaffolding.

Precedence: built-in defaults < global config < repo-local ./magi.toml.
Global path: $MAGI_CONFIG, else $XDG_CONFIG_HOME/magi/config.toml,
else ~/.config/magi/config.toml.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

from .backends import ClaudeCli, CodexCli, GeminiCli
from .council import default_council

BACKENDS = {"claude": ClaudeCli, "codex": CodexCli, "gemini": GeminiCli}
ROLES = ("melchior", "balthasar", "casper")


def global_config_path() -> Path:
    if override := os.environ.get("MAGI_CONFIG"):
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg) / "magi" / "config.toml"


def _member(role: str, spec: dict):
    backend = spec.get("backend", "")
    cls = BACKENDS.get(backend)
    if cls is None:
        raise ValueError(
            f"council.{role}: unknown backend {backend!r} "
            f"(expected one of: {', '.join(BACKENDS)})"
        )
    kwargs = {k: v for k, v in spec.items() if k != "backend"}
    try:  # any backend field is settable; a typo or wrong-backend key lands here
        return cls(**kwargs)
    except TypeError as e:
        raise ValueError(f"council.{role}: {e}") from e


def load_council(repo: Path) -> dict[str, object]:
    council = default_council()
    for path in (global_config_path(), repo / "magi.toml"):  # later wins
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text())
        for role, spec in data.get("council", {}).items():
            if role not in ROLES:
                raise ValueError(f"{path}: unknown council role {role!r}")
            council[role] = _member(role, spec)
    return council


class _Plain:
    """Every color name resolves to an empty string."""

    def __getattr__(self, name: str) -> str:
        return ""


def help_theme():
    """argparse's own palette, or blanks.

    Python 3.14 colors help output and 3.12 does not, so `magi help` follows
    argparse instead of deciding on its own. One colored block under a plain
    help reads worse than no color at all. get_theme() already returns blank
    strings when the stream is not a tty, or when NO_COLOR is set.
    """
    # ponytail: _colorize is private. It is read inside a try that falls back
    # to plain text, so the worst case is an uncolored help, never a crash.
    try:
        from _colorize import get_theme  # 3.13+; the argparse section is 3.14+

        return get_theme().argparse
    except (ImportError, AttributeError):
        return _Plain()


def describe(repo: Path) -> str:
    """The council in use and the files it came from, for `magi help`.

    Reads the same two files as load_council, so what it prints is what the
    next run will convene. A broken config reports its error here instead of
    at the start of a review.
    """
    t = help_theme()
    sources = (global_config_path(), repo / "magi.toml")
    try:
        council = load_council(repo)
    except ValueError as e:
        lines = [f"  {t.label}config error: {e}{t.reset}"]
    else:
        # each field padded inside its own color, so the columns still line up
        lines = [
            f"  {t.action}{role:<10}{t.reset}"
            f" {t.label}{b.name:<7}{t.reset}"
            f" {t.summary_long_option}"
            f"{getattr(b, 'model', None) or '(CLI default)':<16}{t.reset}"
            f" {getattr(b, 'effort', None) or '-'}"
            for role in ROLES
            for b in [council[role]]
        ]
    read = [
        f"    {t.action}✓{t.reset} {p}" if p.is_file() else f"    · {p}"
        for p in sources
    ]
    return "\n".join(
        [
            "council in use — built-in defaults < global < repo-local:",
            *lines,
            f"\n  read from, in that order (for {repo}):",
            *read,
        ]
    )


# --- magi init ----------------------------------------------------------------


def detect_clis() -> set[str]:
    return {cli for cli in BACKENDS if shutil.which(cli)}


def propose(detected: set[str]) -> dict[str, dict]:
    """Council composition for the CLIs actually installed.

    Cross-family diversity when possible: correctness (melchior) sits on a
    different model family than the members judging it. gemini backend is a
    stub, so it is never proposed yet even when detected.
    """
    if {"claude", "codex"} <= detected:
        return {
            "melchior": {"backend": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
            "balthasar": {
                "backend": "claude",
                "model": "claude-opus-5",
                "effort": "xhigh",
            },
            "casper": {
                "backend": "claude",
                "model": "claude-fable-5",
                "effort": "xhigh",
            },
        }
    if "claude" in detected:  # single-family: personas on different models
        return {
            "melchior": {
                "backend": "claude",
                "model": "claude-fable-5",
                "effort": "xhigh",
            },
            "balthasar": {
                "backend": "claude",
                "model": "claude-opus-5",
                "effort": "xhigh",
            },
            "casper": {
                "backend": "claude",
                "model": "claude-opus-4-8",
                "effort": "xhigh",
            },
        }
    if "codex" in detected:
        return {
            role: {"backend": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"}
            for role in ROLES
        }
    raise ValueError(
        "no supported CLI found — install claude (claude.com/claude-code) "
        "or codex (github.com/openai/codex) and re-run"
    )


def render_config(council: dict[str, dict]) -> str:
    lines = [
        "# MAGI council — one table per member.",
        "# backend: claude | codex | gemini(stub)   model/effort: passed to the CLI",
        "# Repo-local ./magi.toml overrides this file; both override built-ins.",
        "",
    ]
    for role in ROLES:
        lines.append(f"[council.{role}]")
        for key, value in council[role].items():
            # json.dumps, not an f-string quote: it renders bool, int and str
            # as valid TOML scalars, and escapes a quote inside a value.
            # `pristine = "False"` would be a truthy string, silently on.
            lines.append(f"{key} = {json.dumps(value)}")
        lines.append("")
    return "\n".join(lines)


def init(dest: Path, detected: set[str] | None = None, force: bool = False) -> str:
    if detected is None:
        detected = detect_clis()
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists — use --force to overwrite")
    text = render_config(propose(detected))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return text
