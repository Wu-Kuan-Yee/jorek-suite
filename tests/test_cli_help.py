#!/usr/bin/env python3
"""Every public CLI action and subcommand must be self-documenting."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import build  # noqa: E402


class CliHelpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = build.make_parser()

    def _walk(self, parser: argparse.ArgumentParser, path: str):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    subpath = path + "/" + name
                    self.assertTrue(
                        subparser.description,
                        "%s has no description" % subpath,
                    )
                    self.assertTrue(subparser.epilog, "%s has no epilog" % subpath)
                    yield from self._walk(subparser, subpath)
            elif action.dest != "help" and not isinstance(action, argparse._VersionAction):
                self.assertTrue(
                    action.help,
                    "%s option %s has no help text"
                    % (path, action.option_strings or action.dest),
                )

    def test_every_action_and_command_is_documented(self) -> None:
        list(self._walk(self.parser, "build.py"))

    def test_top_level_explains_parameter_grids(self) -> None:
        text = self.parser.format_help()
        for needle in ("PARAMETER GRIDS", "--param-max", "--param-grid",
                       "2*(n_tor-1)", "--everything"):
            self.assertIn(needle, text)

    def test_preflight_command_accepts_a_build_plan(self) -> None:
        args = self.parser.parse_args([
            "--work", "/tmp/jorek-preflight-test",
            "preflight", "--for-command", "dist", "--model", "600", "--json",
        ])
        self.assertEqual(args.command, "preflight")
        self.assertEqual(args.for_command, "dist")
        self.assertEqual(args.model, "600")
        self.assertTrue(args.json)

    def test_build_commands_show_parameter_options(self) -> None:
        for name in ("matrix", "build", "dist", "full", "preflight"):
            sub = self.parser._subparsers._group_actions[0].choices[name]
            text = sub.format_help()
            for needle in ("--param-max", "--param-grid", "--set"):
                self.assertIn(needle, text, "%s help lacks %s" % (name, needle))

    def test_all_help_pages_render(self) -> None:
        for name, sub in self.parser._subparsers._group_actions[0].choices.items():
            text = sub.format_help()
            self.assertIn("usage:", text, name)
            self.assertIn("--help", text, name)


if __name__ == "__main__":
    unittest.main()
