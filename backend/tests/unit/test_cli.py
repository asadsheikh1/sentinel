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
