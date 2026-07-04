"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import calendar
import os
import re
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from scanner import VERSION, DB_PATH, XCODE_PROJECTS_DIR

PRICING = {
    # Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
    # (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
}

def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5"]
    if "opus" in m:
        return PRICING["claude-opus-4-8"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db(db_path=None):
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(path)


def resolve_dir_overrides(rest):
    """Resolve --claude-dir / --projects-dir into scan-dir + db-path overrides.

    --projects-dir is a full override of the scan location (single dir, no
    Xcode dir — unchanged existing behavior) and wins over --claude-dir there.
    --claude-dir points at a whole alternate "~/.claude"-shaped directory: it
    scans <dir>/projects additively alongside the Xcode dir (mirroring how
    CLAUDE_CONFIG_DIR relocates Claude Code's own tree) and relocates the DB to
    <dir>/usage.db, unless CLAUDE_USAGE_DB is set — that env var always wins.

    Note: when neither flag is given but CLAUDE_CONFIG_DIR is set, scanning
    already follows it (scanner.PROJECTS_DIR derives from it) while the DB
    default stays put at ~/.claude/usage.db — that asymmetry is intentional,
    not a bug, so an ambient env var set for unrelated reasons can't silently
    relocate an existing installation's database.
    """
    claude_dir = parse_named_arg(rest, "--claude-dir")
    projects_dir_flag = parse_named_arg(rest, "--projects-dir")

    projects_dirs = None
    db_path = None

    if claude_dir:
        base = Path(claude_dir)
        projects_dirs = [base / "projects", XCODE_PROJECTS_DIR]
        if not os.environ.get("CLAUDE_USAGE_DB"):
            db_path = base / "usage.db"

    projects_dir = Path(projects_dir_flag) if projects_dir_flag else None
    if projects_dir:
        projects_dirs = None  # --projects-dir fully overrides, dropping Xcode

    return projects_dir, projects_dirs, db_path


def positional_args(args, flags=("--claude-dir", "--projects-dir"), bool_flags=("--scan",)):
    """Return `args` with any recognized `--flag value` pairs and boolean flags stripped out."""
    result, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in flags:
            skip = True
            continue
        if a in bool_flags:
            continue
        result.append(a)
    return result


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None, projects_dirs=None, db_path=None):
    from scanner import scan
    scan(
        projects_dir=Path(projects_dir) if projects_dir else None,
        projects_dirs=projects_dirs,
        db_path=Path(db_path) if db_path else DB_PATH,
        verbose=True,
    )


def usage_by_model(conn, start, end):
    """By-model token/turn breakdown for [start, end] inclusive (YYYY-MM-DD)."""
    return conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()


def usage_by_bucket(conn, start, end, bucket_len):
    """By-<bucket>-and-model breakdown. bucket_len=10 groups by day
    (substr(timestamp,1,10)); bucket_len=7 groups by month (substr(...,1,7))."""
    return conn.execute(f"""
        SELECT
            substr(timestamp, 1, {bucket_len}) as bucket,
            COALESCE(model, 'unknown')          as model,
            SUM(input_tokens)                   as inp,
            SUM(output_tokens)                  as out,
            SUM(cache_read_tokens)               as cr,
            SUM(cache_creation_tokens)           as cc,
            COUNT(*)                             as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY bucket, model
    """, (start, end)).fetchall()


def session_count(conn, start, end):
    row = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return row["cnt"]


def print_by_model(rows, indent="  "):
    """Print each model's row plus a TOTAL line; return totals for the footer."""
    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"{indent}{r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")
    hr()
    print(f"{indent}{'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    return {"inp": total_inp, "out": total_out, "cr": total_cr, "cc": total_cc,
            "turns": total_turns, "cost": total_cost}


def print_by_bucket(rows, buckets, indent="    "):
    """Print one line per bucket key in `buckets` order, zero-filling gaps."""
    per_bucket = {}
    for r in rows:
        acc = per_bucket.setdefault(r["bucket"], {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        acc["turns"] += r["turns"]
        acc["inp"]   += r["inp"] or 0
        acc["out"]   += r["out"] or 0
        acc["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
    for b in buckets:
        acc = per_bucket.get(b, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"{indent}{b}  turns={acc['turns']:<4}  in={fmt(acc['inp']):<8}  out={fmt(acc['out']):<8}  cost={fmt_cost(acc['cost'])}")


def day_range(start, end):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


def month_range(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    months = []
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def cmd_today(db_path=None):
    conn = require_db(db_path)
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = usage_by_model(conn, today, today)

    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
          AND COALESCE(is_subagent, 0) = 1
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        conn.close()
        return

    totals = print_by_model(rows)
    sess_cnt = session_count(conn, today, today)

    print()
    print(f"  Sessions today:   {sess_cnt}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cache read:       {fmt(totals['cr'])}")
    print(f"  Cache creation:   {fmt(totals['cc'])}")
    hr()
    print()
    conn.close()


def cmd_week(db_path=None):
    conn = require_db(db_path)
    conn.row_factory = sqlite3.Row

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = usage_by_bucket(conn, start, end, 10)
    by_model = usage_by_model(conn, start, end)

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    print("  By Day:")
    print_by_bucket(by_day_model, day_range(start, end))

    hr()
    print("  By Model:")
    totals = print_by_model(by_model, indent="    ")
    sess_cnt = session_count(conn, start, end)

    print()
    print(f"  Sessions this week:  {sess_cnt}")
    print(f"  Cache read:          {fmt(totals['cr'])}")
    print(f"  Cache creation:      {fmt(totals['cc'])}")
    hr()
    print()
    conn.close()


def cmd_month(db_path=None):
    conn = require_db(db_path)
    conn.row_factory = sqlite3.Row

    today_d = date.today()
    start_d = today_d.replace(day=1)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = usage_by_bucket(conn, start, end, 10)
    by_model = usage_by_model(conn, start, end)

    print()
    hr()
    print(f"  Month-to-Date Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded this month.")
        print()
        conn.close()
        return

    print("  By Day:")
    print_by_bucket(by_day_model, day_range(start, end))

    hr()
    print("  By Model:")
    totals = print_by_model(by_model, indent="    ")
    sess_cnt = session_count(conn, start, end)

    print()
    print(f"  Sessions this month:  {sess_cnt}")
    print(f"  Cache read:           {fmt(totals['cr'])}")
    print(f"  Cache creation:       {fmt(totals['cc'])}")
    hr()
    print()
    conn.close()


def parse_range_arg(args):
    """Parse `range`'s 1-2 positional args into (start, end) inclusive ISO dates.

    Accepts a single YYYY (year), YYYY-MM (month), or YYYY-MM-DD (day), or two
    YYYY-MM-DD dates for an explicit range. Raises ValueError on anything else.
    """
    if len(args) == 1:
        a = args[0]
        if re.fullmatch(r"\d{4}", a):
            year = int(a)
            date(year, 1, 1)  # validates the year is representable
            return f"{year:04d}-01-01", f"{year:04d}-12-31"
        if re.fullmatch(r"\d{4}-\d{2}", a):
            year, month = int(a[:4]), int(a[5:7])
            last_day = calendar.monthrange(year, month)[1]  # raises on bad month
            return f"{a}-01", f"{a}-{last_day:02d}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
            date.fromisoformat(a)
            return a, a
        raise ValueError(f"invalid date/range {a!r}")

    if len(args) == 2:
        for a in args:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
                raise ValueError(f"invalid date {a!r} (expected YYYY-MM-DD)")
        start, end = args
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end:
            raise ValueError(f"start date {start} is after end date {end}")
        return start, end

    raise ValueError("range takes 1 or 2 arguments")


def cmd_range(*args, db_path=None):
    try:
        start, end = parse_range_arg(args)
    except ValueError as e:
        print(f"Error: {e}")
        print("Usage: python cli.py range YYYY | YYYY-MM | YYYY-MM-DD [YYYY-MM-DD]")
        sys.exit(1)

    conn = require_db(db_path)
    conn.row_factory = sqlite3.Row

    span_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    by_month = span_days > 31
    bucket_len = 7 if by_month else 10

    by_bucket = usage_by_bucket(conn, start, end, bucket_len)
    by_model = usage_by_model(conn, start, end)

    print()
    hr()
    print(f"  Usage Report  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in this range.")
        print()
        conn.close()
        return

    print(f"  By {'Month' if by_month else 'Day'}:")
    buckets = month_range(start, end) if by_month else day_range(start, end)
    print_by_bucket(by_bucket, buckets)

    hr()
    print("  By Model:")
    totals = print_by_model(by_model, indent="    ")
    sess_cnt = session_count(conn, start, end)

    print()
    print(f"  Sessions:             {sess_cnt}")
    print(f"  Cache read:           {fmt(totals['cr'])}")
    print(f"  Cache creation:       {fmt(totals['cc'])}")
    hr()
    print()
    conn.close()


def cmd_stats(db_path=None):
    conn = require_db(db_path)
    conn.row_factory = sqlite3.Row

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
    """).fetchone()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(projects_dir=None, projects_dirs=None, db_path=None,
                   host=None, port=None, no_browser=False, surface=None):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, and
    # the VS Code extension kills the process if it doesn't answer /api/data
    # within ~10s (see vscode-extension/src/server-manager.ts). Serving up front
    # means the port is live immediately; the dashboard shows whatever's already
    # in the DB and auto-refreshes as the background scan commits new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        scan(projects_dir=projects_dir, projects_dirs=projects_dirs, db_path=db_path)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README). The VS Code
    # extension passes --no-browser since it embeds the dashboard in a webview.
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, surface=surface, db_path=db_path, projects_dirs=projects_dirs)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH] [--claude-dir PATH]
                                              Scan JSONL files and update database
  python cli.py today [--scan]               Show today's usage summary
  python cli.py week [--scan]                Show last 7 days (per-day + by-model)
  python cli.py month [--scan]               Show month-to-date usage (per-day + by-model)
  python cli.py range YYYY | YYYY-MM | YYYY-MM-DD [YYYY-MM-DD] [--scan]
                                              Show usage for a year, month, day, or explicit range
  python cli.py stats [--scan]               Show all-time statistics
  python cli.py dashboard [--projects-dir PATH] [--claude-dir PATH] [--host HOST] [--port PORT] [--no-browser] [--surface SURFACE]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py --version                    Print the version and exit

  --claude-dir PATH   Point every command at an arbitrary Claude Code home
                       directory (scans PATH/projects, database at PATH/usage.db).
                       Also auto-detected via the CLAUDE_CONFIG_DIR env var for
                       scanning (see README). CLAUDE_USAGE_DB always wins for
                       the database location if set.
  --scan               Rescan before running a report command (today/week/month/
                       range/stats), so the output reflects the latest transcripts
                       without a separate `scan` call first. No effect on `scan`
                       or `dashboard`, which already scan.
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "month": cmd_month,
    "range": cmd_range,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    """Console entry point (``claude-usage``) and ``python cli.py`` dispatch."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir, projects_dirs, db_path = resolve_dir_overrides(rest)

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            projects_dirs=projects_dirs,
            db_path=db_path,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
            surface=parse_named_arg(rest, "--surface"),
        )
    elif command == "scan":
        cmd_scan(projects_dir=projects_dir, projects_dirs=projects_dirs, db_path=db_path)
    else:
        if "--scan" in rest:
            cmd_scan(projects_dir=projects_dir, projects_dirs=projects_dirs, db_path=db_path)
            print()
        if command == "range":
            cmd_range(*positional_args(rest), db_path=db_path)
        else:
            COMMANDS[command](db_path=db_path)


if __name__ == "__main__":
    main()
