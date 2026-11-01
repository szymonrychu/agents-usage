import os

import pytest

from agents_usage import __version__, cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("USAGE_"):
            monkeypatch.delenv(key)


def test_bad_config_exits_78(monkeypatch, capsys):
    assert cli.main(["migrate"]) == 78
    assert "USAGE_DATABASE_URL" in capsys.readouterr().err


def test_commands_registered():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert {"migrate", "api"} <= set(sub.choices)


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_registered_command_dispatches():
    seen = []

    def func(args):
        seen.append(args.value)
        return 7

    def configure(parser):
        parser.add_argument("--value", default="x")

    cli.register("probe-test", func, "test command", configure)
    assert cli.main(["probe-test", "--value", "v"]) == 7
    assert seen == ["v"]


@pytest.mark.parametrize("argv", [[], ["nope"]])
def test_unknown_command_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in (captured.out + captured.err).lower()


def test_prober_registered():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert "prober" in sub.choices


def test_prober_needs_token_file(monkeypatch, capsys):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://u:p@db.invalid/usage")
    assert cli.main(["prober"]) == 78
    assert "USAGE_CLAUDE_TOKEN_FILE: required for the prober" in capsys.readouterr().err
