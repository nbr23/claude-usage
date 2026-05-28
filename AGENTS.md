# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

## Project shape

Three Python files, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py dashboard             # scan + open http://localhost:8080
python cli.py scan --projects-dir PATH    # scan a custom transcripts dir
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in two places that **must stay in sync**:
- [cli.py](cli.py) `PRICING` dict (Python)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` and are billed at $0 (shown as `n/a`) — this is intentional so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

### Dashboard server

`http.server.BaseHTTPRequestHandler`-based, two endpoints:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model.
- `POST /api/rescan` → deletes the DB and runs a full rescan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.

This applies to all agents working on this repo, not just Claude Code.

## Versioning and releases

This project follows [SemVer](https://semver.org/) with two conventions specific to how releases are surfaced:

1. **Tags always.** Every version that lands on `main` gets an annotated git tag (`git tag -a vX.Y.Z -m "vX.Y.Z"`). No exceptions for patch versions. The tag goes on the merge commit, not on individual contributor commits inside the merge bubble. Tags pay off in three places: Homebrew formula pinning (`archive/refs/tags/vX.Y.Z.tar.gz` is stabler than commit SHAs), `git log vX.Y.Z-1..vX.Y.Z` for changelog work, and `gh release create <tag>` later if a release ever needs to be promoted retroactively.

2. **Formal GitHub Releases only for major versions.** Patch (`vX.Y.Z` where `Z` increments) and minor (`Y` increments) bumps ship as a tag and a CHANGELOG entry — that's the entire release process. Formal `gh release create` with release notes, asset uploads, and the "Latest release" badge is reserved for major versions (`X` increments), where breaking changes or significant new surface area warrant a notification to people who follow only Releases.

Why: patches and minors are the quiet bug-fix-and-improve cadence; CHANGELOG.md is the canonical record. Majors are the "you should read this before upgrading" events; that's what Releases are for.

When the maintainer says "release v1.1.1", the steps are:
- Finalize CHANGELOG (replace `TBD` with today's date)
- Merge DEV → main (`merge --no-ff` so the release boundary is visible in `git log main`)
- `git tag -a v1.1.1 -m "v1.1.1"` on the merge commit
- `git push origin main --follow-tags`
- No `gh release create` call (that's for majors)

Tags are **annotated** (`git tag -a`, not lightweight) and are created **only** on `main`'s release merge commits — never on individual contributor commits inside a merge bubble, never on `DEV`.

Note: v1.1.0 shipped before this convention was documented and therefore has no tag. The convention applies forward from v1.1.1. Don't retroactively tag v1.1.0 without explicit maintainer ask.

### Homebrew formula and self-referential SHA

The Homebrew formula at `Formula/claude-usage.rb` lives inside this same repo. Be careful when bumping it: if the formula's `url` points at a tarball that **contains the formula itself with that sha256**, the sha256 is self-referential and uncomputable. Practical rule: a release's formula must point at the **previous** release's tarball, never its own. In v1.1.1 the formula points at v1.1.0's commit-SHA tarball, so brew users installing v1.1.1's formula receive v1.1.0 code — that's the trade-off of keeping the formula in-tree.

## Weekly triage routine

The repo has a self-contained slash command at [.claude/commands/triage.md](.claude/commands/triage.md) that automates the weekly PR/issue cleanup we used to ship v1.1.0: classify open items with Codex, merge no-brainers to DEV preserving authorship, run tests, close duplicates / scope-violations with friendly messages, bump CHANGELOG by patch, push DEV. **The routine never pushes to `main`** — release decisions stay with the maintainer.

Register the Windows Task Scheduler entry with [scripts/setup-weekly-triage.ps1](scripts/setup-weekly-triage.ps1). Logs go to `logs/triage-*.log`.

If you're working on this repo and want to invoke the routine ad-hoc, just type `/triage` in Claude Code. Hard safety rails (test-passing gates, no security-sensitive auto-merges, no scope-changing merges, Codex sign-off required on closures) live inside `triage.md`.
