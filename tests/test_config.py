"""Config loading and `magi init` — synthetic, no CLI detection or live calls."""

import pytest

from magi.backends import ClaudeCli, CodexCli
from magi.config import init, load_council, propose


def test_defaults_when_no_config_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    council = load_council(tmp_path)
    assert set(council) == {"melchior", "balthasar", "casper"}
    assert isinstance(council["melchior"], CodexCli)


def test_repo_config_overrides_global(tmp_path, monkeypatch):
    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text('[council.melchior]\nbackend = "claude"\nmodel = "from-global"\n')
    monkeypatch.setenv("MAGI_CONFIG", str(global_cfg))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "magi.toml").write_text(
        '[council.melchior]\nbackend = "claude"\nmodel = "from-repo"\neffort = "high"\n'
    )
    council = load_council(repo)
    assert isinstance(council["melchior"], ClaudeCli)
    assert council["melchior"].model == "from-repo"
    assert council["melchior"].effort == "high"
    # roles not mentioned anywhere keep built-in defaults
    assert council["balthasar"].model == "claude-opus-5"


def test_unknown_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "magi.toml").write_text('[council.melchior]\nbackend = "gpt4all"\n')
    with pytest.raises(ValueError, match="unknown backend"):
        load_council(tmp_path)


def test_backend_field_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "magi.toml").write_text(
        '[council.melchior]\nbackend = "codex"\nservice_tier = "priority"\n'
    )
    assert load_council(tmp_path)["melchior"].service_tier == "priority"


def test_field_unknown_to_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "magi.toml").write_text(
        '[council.melchior]\nbackend = "claude"\nservice_tier = "priority"\n'
    )
    with pytest.raises(ValueError, match="council.melchior"):
        load_council(tmp_path)


def test_unknown_role_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "magi.toml").write_text('[council.overlord]\nbackend = "claude"\n')
    with pytest.raises(ValueError, match="unknown council role"):
        load_council(tmp_path)


def test_propose_compositions():
    both = propose({"claude", "codex"})
    assert both["melchior"]["backend"] == "codex"  # cross-family correctness seat
    assert both["casper"]["backend"] == "claude"

    claude_only = propose({"claude"})
    assert {m["backend"] for m in claude_only.values()} == {"claude"}
    assert len({m["model"] for m in claude_only.values()}) == 3  # distinct models

    codex_only = propose({"codex"})
    assert {m["backend"] for m in codex_only.values()} == {"codex"}

    with pytest.raises(ValueError, match="no supported CLI"):
        propose(set())

    # gemini backend is a stub: never proposed even when detected
    with_gemini = propose({"claude", "codex", "gemini"})
    assert all(m["backend"] != "gemini" for m in with_gemini.values())


def test_init_writes_and_respects_force(tmp_path):
    dest = tmp_path / "cfg" / "config.toml"
    text = init(dest, detected={"claude", "codex"})
    assert dest.read_text() == text
    assert "[council.melchior]" in text
    with pytest.raises(FileExistsError, match="--force"):
        init(dest, detected={"claude"})
    init(dest, detected={"claude"}, force=True)
    assert 'backend = "claude"' in dest.read_text()


def test_main_config_error_exits_3_not_1(tmp_path, monkeypatch, capsys):
    """Exit 1 means REQUEST_CHANGES in CI; config errors must use 3."""
    (tmp_path / "magi.toml").write_text('[council.melchior]\nbackend = "nope"\n')
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr("sys.argv", ["magi", str(tmp_path)])
    from magi.tui import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    assert "config error" in capsys.readouterr().err


def test_init_output_roundtrips_through_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "absent.toml"))
    init(tmp_path / "magi.toml", detected={"claude", "codex"})
    council = load_council(tmp_path)
    assert isinstance(council["melchior"], CodexCli)
    assert council["melchior"].model == "gpt-5.6-sol"
    assert council["melchior"].effort == "xhigh"
    assert isinstance(council["casper"], ClaudeCli)
    assert council["casper"].model == "claude-fable-5"


def test_render_config_writes_toml_scalars():
    """A bool must stay a bool. `pristine = "False"` is a truthy string, and
    it would turn the setting silently on."""
    import tomllib

    from magi.config import ROLES, render_config

    text = render_config({r: {"backend": "claude", "pristine": False} for r in ROLES})
    melchior = tomllib.loads(text)["council"]["melchior"]
    assert melchior["pristine"] is False
    assert melchior["backend"] == "claude"


def test_describe_names_the_council_and_its_sources(tmp_path, monkeypatch):
    """`magi help` must say which council the next run will convene."""
    from magi.config import describe

    absent = tmp_path / "none.toml"
    monkeypatch.setenv("NO_COLOR", "1")  # 3.14 colours help; assert the plain form
    monkeypatch.setenv("MAGI_CONFIG", str(absent))
    (tmp_path / "magi.toml").write_text(
        '[council.melchior]\nbackend = "claude"\nmodel = "from-repo"\n'
    )
    text = describe(tmp_path)
    assert "melchior   claude  from-repo" in text  # repo-local wins
    assert "balthasar" in text and "casper" in text  # built-in defaults for the rest
    assert f"· {absent}" in text  # marked absent
    assert f"✓ {tmp_path / 'magi.toml'}" in text  # marked read


def test_describe_reports_a_broken_config_instead_of_raising(tmp_path, monkeypatch):
    from magi.config import describe

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("MAGI_CONFIG", str(tmp_path / "none.toml"))
    (tmp_path / "magi.toml").write_text('[council.melchior]\nbackend = "nonesuch"\n')
    assert "config error" in describe(tmp_path)
    assert "unknown backend" in describe(tmp_path)
