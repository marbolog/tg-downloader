"""Thin coverage for main.py's command dispatch -- matches the existing
(implicit, untested) coverage level of `discard`'s dispatch: this only
proves `browse` is wired to build_parser() and recognized as a DB-only
command, not a full behavioral test (that's tests/test_reader.py's job)."""
from main import build_parser


def test_browse_is_a_recognized_subcommand():
    parser = build_parser()
    args = parser.parse_args(["browse"])
    assert args.command == "browse"
