"""Tests for cli.py - directory/DB overrides and the month/range commands."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import cli
from scanner import get_db, init_db, insert_turns


def _turn(session_id, ts, model, inp, out, message_id):
    return {
        "session_id": session_id, "timestamp": ts, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj", "message_id": message_id,
    }


class TestResolveDirOverrides(unittest.TestCase):
    def test_no_flags_returns_all_none(self):
        projects_dir, projects_dirs, db_path = cli.resolve_dir_overrides([])
        self.assertIsNone(projects_dir)
        self.assertIsNone(projects_dirs)
        self.assertIsNone(db_path)

    def test_claude_dir_derives_projects_dirs_and_db_path(self):
        projects_dir, projects_dirs, db_path = cli.resolve_dir_overrides(
            ["--claude-dir", "/tmp/myclaude"])
        self.assertIsNone(projects_dir)
        self.assertEqual(projects_dirs, [Path("/tmp/myclaude/projects"), cli.XCODE_PROJECTS_DIR])
        self.assertEqual(db_path, Path("/tmp/myclaude/usage.db"))

    def test_projects_dir_full_override_no_db_path(self):
        projects_dir, projects_dirs, db_path = cli.resolve_dir_overrides(
            ["--projects-dir", "/tmp/custom"])
        self.assertEqual(projects_dir, Path("/tmp/custom"))
        self.assertIsNone(projects_dirs)
        self.assertIsNone(db_path)

    def test_projects_dir_wins_over_claude_dir_for_scan_location(self):
        projects_dir, projects_dirs, db_path = cli.resolve_dir_overrides(
            ["--claude-dir", "/tmp/myclaude", "--projects-dir", "/tmp/custom"])
        self.assertEqual(projects_dir, Path("/tmp/custom"))
        self.assertIsNone(projects_dirs)
        # --claude-dir's db_path derivation still applies even though its
        # projects_dirs got overridden by the more specific --projects-dir.
        self.assertEqual(db_path, Path("/tmp/myclaude/usage.db"))

    def test_claude_usage_db_env_wins_over_claude_dir(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_DB": "/custom/db.sqlite"}):
            _, _, db_path = cli.resolve_dir_overrides(["--claude-dir", "/tmp/myclaude"])
        self.assertIsNone(db_path)


class TestPositionalArgs(unittest.TestCase):
    def test_strips_claude_dir_flag(self):
        self.assertEqual(
            cli.positional_args(["--claude-dir", "/tmp/x", "2026-06"]),
            ["2026-06"])

    def test_strips_projects_dir_flag(self):
        self.assertEqual(
            cli.positional_args(["--projects-dir", "/tmp/x", "2026"]),
            ["2026"])

    def test_no_flags_passthrough(self):
        self.assertEqual(
            cli.positional_args(["2026-06-01", "2026-06-15"]),
            ["2026-06-01", "2026-06-15"])

    def test_strips_scan_flag(self):
        self.assertEqual(
            cli.positional_args(["2026-06", "--scan"]),
            ["2026-06"])

    def test_strips_scan_flag_mixed_with_claude_dir(self):
        self.assertEqual(
            cli.positional_args(["--scan", "--claude-dir", "/tmp/x", "2026"]),
            ["2026"])


class TestMainScanFlag(unittest.TestCase):
    def _run_main(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf):
            cli.main()
        return buf.getvalue()

    def test_scan_flag_rescans_before_report(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "projects").mkdir()
        out = self._run_main(["cli.py", "stats", "--scan", "--claude-dir", str(tmp)])
        self.assertIn("Scan complete", out)
        self.assertIn("All-Time Statistics", out)
        self.assertTrue((tmp / "usage.db").exists())

    def test_without_scan_flag_fails_on_missing_db(self):
        tmp = Path(tempfile.mkdtemp())
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["cli.py", "stats", "--claude-dir", str(tmp)]), \
                redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertIn("Database not found", buf.getvalue())

    def test_scan_flag_does_not_break_range_date_parsing(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "projects").mkdir()
        out = self._run_main(["cli.py", "range", "2026", "--scan", "--claude-dir", str(tmp)])
        self.assertIn("Scan complete", out)
        self.assertIn("2026-01-01 to 2026-12-31", out)


class TestCmdScanThreading(unittest.TestCase):
    def test_threads_overrides_into_scan(self):
        with mock.patch("scanner.scan") as mock_scan:
            cli.cmd_scan(projects_dirs=[Path("/tmp/a")], db_path=Path("/tmp/a/usage.db"))
        mock_scan.assert_called_once_with(
            projects_dir=None, projects_dirs=[Path("/tmp/a")],
            db_path=Path("/tmp/a/usage.db"), verbose=True)


class TestParseRangeArg(unittest.TestCase):
    def test_year(self):
        self.assertEqual(cli.parse_range_arg(["2024"]), ("2024-01-01", "2024-12-31"))

    def test_month(self):
        self.assertEqual(cli.parse_range_arg(["2026-06"]), ("2026-06-01", "2026-06-30"))

    def test_day(self):
        self.assertEqual(cli.parse_range_arg(["2026-06-15"]), ("2026-06-15", "2026-06-15"))

    def test_explicit_range(self):
        self.assertEqual(
            cli.parse_range_arg(["2026-06-01", "2026-06-15"]),
            ("2026-06-01", "2026-06-15"))

    def test_invalid_token_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["foo"])

    def test_invalid_month_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["2024-13"])

    def test_invalid_day_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["2024-02-30"])

    def test_two_non_date_args_raise(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["a", "b"])

    def test_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["2026-06-15", "2026-06-01"])

    def test_no_args_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg([])

    def test_too_many_args_raises(self):
        with self.assertRaises(ValueError):
            cli.parse_range_arg(["a", "b", "c"])


class TestCmdMonthAndRange(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _seed(self, turns):
        conn = get_db(self.db_path)
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def test_month_shows_month_to_date(self):
        today = date.today().isoformat()
        self._seed([_turn("s1", today + "T10:00:00Z", "claude-sonnet-4-6", 100, 50, "m1")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_month()
        out = buf.getvalue()
        self.assertIn("Month-to-Date Usage", out)
        self.assertIn("By Day:", out)
        self.assertIn("Sessions this month:", out)

    def test_range_month_buckets_by_day(self):
        self._seed([
            _turn("s1", "2026-06-01T10:00:00Z", "claude-sonnet-4-6", 100, 50, "m1"),
            _turn("s1", "2026-06-15T10:00:00Z", "claude-opus-4-8", 200, 80, "m2"),
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_range("2026-06")
        out = buf.getvalue()
        self.assertIn("By Day:", out)
        self.assertNotIn("By Month:", out)
        self.assertIn("2026-06-01", out)
        self.assertIn("2026-06-30", out)

    def test_range_year_buckets_by_month(self):
        self._seed([
            _turn("s1", "2024-03-15T10:00:00Z", "claude-sonnet-4-6", 100, 50, "m1"),
            _turn("s1", "2024-11-01T10:00:00Z", "claude-opus-4-8", 200, 80, "m2"),
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_range("2024")
        out = buf.getvalue()
        self.assertIn("By Month:", out)
        self.assertNotIn("By Day:", out)
        self.assertIn("2024-03", out)
        self.assertIn("2024-11", out)

    def test_range_explicit_dates(self):
        self._seed([
            _turn("s1", "2026-06-01T10:00:00Z", "claude-sonnet-4-6", 100, 50, "m1"),
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_range("2026-06-01", "2026-06-15")
        out = buf.getvalue()
        self.assertIn("(2026-06-01 to 2026-06-15)", out)

    def test_range_no_data_message(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_range("2020")
        out = buf.getvalue()
        self.assertIn("No usage recorded in this range.", out)

    def test_range_invalid_input_exits_nonzero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_range("not-a-date")
        self.assertNotEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("Error:", out)
        self.assertIn("Usage:", out)


if __name__ == "__main__":
    unittest.main()
