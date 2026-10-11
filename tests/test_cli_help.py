import argparse

import pytest

from meshgarden.main import build_parser, main


def _walk(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield sub
                yield from _walk(sub)


def test_every_subparser_has_description():
    parsers = list(_walk(build_parser()))
    assert parsers
    missing = [p.prog for p in parsers if not (p.description or "").strip()]
    assert missing == []


def test_top_level_help_has_groups_and_examples(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for group in ("Run & health", "Watch traffic", "Visitors", "Games", "Data"):
        assert group in out
    assert "mgarden tail -n 200" in out


def test_help_command_prints_command_help(capsys):
    assert main(["help", "tail"]) == 0
    assert "--from-start" in capsys.readouterr().out


def test_help_unknown_command_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["help", "bogus"])
    assert exc.value.code == 2
    assert "tail" in capsys.readouterr().err


def test_no_command_prints_help_and_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "Command groups" in capsys.readouterr().err


def test_tail_lines_parses():
    assert build_parser().parse_args(["tail", "-n", "200"]).lines == 200
