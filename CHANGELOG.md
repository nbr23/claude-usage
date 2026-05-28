# Changelog

## v1.1.0 — 2026-05-28

- Fix `ReferenceError: cutoff is not defined` that blanked the dashboard whenever the hourly filter ran (#73, thanks @thomasleveil)
- Fix hourly chart ignoring the range upper bound for `week` / `month` / `prev-month` ranges
- Fix 404 on dashboard URLs containing query strings (`?range=...&models=...`) so reloads and bookmarks work (#81, thanks @jakduch)
- Fix incremental scan not updating `first_timestamp` when a newly discovered session's records arrive out of order (#111, thanks @Fruhji)
- Use `ThreadingHTTPServer` so a slow `/api/data` no longer blocks other dashboard requests (#79, thanks @jakduch)
- Adopt `AGENTS.md` (shared with Codex); `CLAUDE.md` is now a thin `@AGENTS.md` import
- Drop unused `.claude/launch.json`

## v1.0.0 — 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
