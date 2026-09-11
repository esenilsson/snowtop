import unittest
from unittest.mock import patch

from snowtop.app import _rows_from_cursor, copy_to_system_clipboard, parse_args, safe_terminal_text


class Cursor:
    description = [("QUERY_TEXT",)]

    @staticmethod
    def fetchall():
        return [("select '\x1b]52;c;SGk=\x07'",)]


class SecurityTests(unittest.TestCase):
    def test_terminal_control_codes_are_removed_from_cursor_results(self):
        rows = _rows_from_cursor(Cursor())
        self.assertEqual(rows, [{"query_text": "select ']52;c;SGk='"}])
        self.assertEqual(safe_terminal_text("hello\nworld\t!"), "hello\nworld\t!")

    def test_resource_limits_are_validated(self):
        for arguments in (
            ["--limit", "0"],
            ["--limit", "10001"],
            ["--since", "8d"],
            ["--interval", "0.5"],
        ):
            with self.assertRaises(SystemExit) as error:
                parse_args(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_credential_cache_can_be_disabled(self):
        self.assertTrue(parse_args(["--no-credential-cache"]).no_credential_cache)

    def test_history_defaults_to_a_one_shot_snapshot(self):
        self.assertTrue(parse_args(["--history"]).once)
        self.assertFalse(parse_args(["--history", "--interactive"]).once)

    @patch("snowtop.app.subprocess.run")
    @patch("snowtop.app.sys.platform", "darwin")
    def test_copy_uses_the_macos_system_clipboard(self, run):
        self.assertTrue(copy_to_system_clipboard("select 1"))
        run.assert_called_once_with(
            ["pbcopy"], input="select 1", text=True, check=True, timeout=5
        )
