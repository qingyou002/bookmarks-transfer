import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from browser_transfer.cli import console_prompt, main
from browser_transfer.bookmarks import Comparison
from browser_transfer.models import Profile
from .test_bookmarks import bar, node


class CLITests(unittest.TestCase):
    def test_default_and_archive_flags_reach_engine(self):
        edge, chrome = Profile("Edge", Path("unused-edge"), "Default"), Profile("Chrome", Path("unused-chrome"), "Default")
        for arguments, mode in (([], "bar"), (["--bookmarks-mode", "archive"], "archive")):
            engine = Mock()
            engine.migrate.return_value = {"results": {}}
            with patch("browser_transfer.cli.require_venv"), \
                 patch("browser_transfer.cli.discover", return_value={"Edge": [edge], "Chrome": [chrome]}), \
                 patch("browser_transfer.cli.Engine", return_value=engine), patch("builtins.print"):
                self.assertEqual(main(["migrate", "--items", "bookmarks", *arguments]), 0)
            engine.migrate.assert_called_once_with(edge, chrome, ["bookmarks"], bookmarks_mode=mode)

    def test_console_bookmark_target_base_and_exception(self):
        comparison = Comparison(bar(), bar(node(20, "A", "https://a.invalid")), "edge", "chrome")
        request = {"kind": "bookmark_compare", "message": "compare", "comparison": comparison,
                   "source_browser": "Edge", "target_browser": "Chrome"}
        with patch("builtins.input", side_effect=["t", "y"]), patch("builtins.print"):
            self.assertEqual(console_prompt(request), {"base": "target", "choices": {}})
        with patch("builtins.input", side_effect=["1t", "y"]), patch("builtins.print"):
            answer = console_prompt(request)
        self.assertEqual(comparison.resolve(answer)["preview"]["deleted"], 0)

    def test_console_bar_selection_and_force_gating(self):
        request = {"kind": "bookmark_bars", "message": "bars", "source_bars": [bar()],
                   "target_bars": [bar(), {"id": "9", "title": "account", "children": []}],
                   "source_browser": "Edge", "target_browser": "Chrome"}
        with patch("builtins.input", side_effect=["1", "2"]), patch("builtins.print"):
            self.assertEqual(console_prompt(request), {"source": "1", "target": "9"})
        for can_force, result in ((False, "cancel"), (True, "close_force")):
            with patch("builtins.input", return_value="f"), patch("builtins.print"):
                self.assertEqual(console_prompt({"kind": "close", "message": "close", "can_force": can_force}), result)
