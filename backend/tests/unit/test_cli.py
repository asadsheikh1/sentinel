import pytest

from sentinel import __version__
from sentinel.cli import build_parser


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_api_command_parses() -> None:
    args = build_parser().parse_args(["api", "--port", "9000", "--reload"])
    assert args.command == "api"
    assert args.port == 9000
    assert args.reload is True


def test_db_commands_parse() -> None:
    parser = build_parser()
    assert parser.parse_args(["db", "upgrade"]).revision == "head"
    assert parser.parse_args(["db", "downgrade", "-1"]).revision == "-1"
    assert parser.parse_args(["db", "revision", "-m", "add x"]).message == "add x"
    docs = parser.parse_args(["db", "docs", "--check"])
    assert docs.check is True


def test_db_docs_command_writes_file(tmp_path: pytest.TempPathFactory) -> None:
    from sentinel.cli import main

    out = f"{tmp_path}/database.md"
    assert main(["db", "docs", "--output", out]) == 0
    assert main(["db", "docs", "--output", out, "--check"]) == 0
