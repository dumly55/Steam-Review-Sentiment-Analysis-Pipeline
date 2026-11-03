# Steam Review Sentiment Analysis Pipeline

**LLM-powered player feedback analytics for Steam game updates.** Python ETL · PostgreSQL · SQL · local NLP with Ollama · Streamlit · Power BI

> **Status: Phase 2 complete (update catalog and feedback windows).** The control panel loads every Helldivers 2 release from Steam, lets you select updates, and freezes each one's feedback window when a run starts. Feedback is not collected or analyzed yet, so there are no findings. See [docs/source_findings.md](docs/source_findings.md) for what was verified against live Steam data.

## What it will do

Pick one or more Steam game updates (default game: Helldivers 2, app ID 553850). For each update, the pipeline will:

1. **Extract**: collect Steam reviews and update-page comments posted while that update was the current one, up to the next release.
2. **Transform**: use a local LLM (Ollama) to pull out which features players praised or criticized, with exact quoted evidence.
3. **Load and aggregate**: store everything in PostgreSQL, where SQL counts distinct authors per feature and assigns reception labels using fixed rules.
4. **Visualize**: show results in a Power BI dashboard.

No paid AI APIs. Everything runs locally.

## Tech stack

| Layer | Tool |
|---|---|
| Ingestion / ETL | Python, Steam Web API |
| Storage and analysis | PostgreSQL, SQL views |
| NLP / sentiment analysis | Ollama (local LLM, e.g. Qwen3 8B), structured JSON output |
| Control panel | Streamlit |
| Reporting | Power BI Desktop |
| Testing | pytest |

## Project layout

```
steam_analyzer/   Python package
  catalog.py        release detection and feedback-window rules (pure functions)
  catalog_store.py  catalog refresh and storage in PostgreSQL
  runs.py           run creation: frozen cutoff, timeline and windows
  steam_sources.py  Steam reviews, events and comment-page access
  app.py            Streamlit control panel
sql/              PostgreSQL schema and, later, reporting views
config/           manual release corrections (release_overrides.json)
tests/            pytest tests; fixtures are labeled synthetic or real snapshot
docs/             Verified source findings
```

## Setup (Windows)

Requires Python 3.12 from python.org, PostgreSQL 17 and Ollama.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then set your postgres password
ollama pull qwen3:8b
python -m steam_analyzer initdb
```

## Commands

```powershell
python -m steam_analyzer initdb      # create the database if missing and apply sql/schema.sql (safe to rerun)
python -m steam_analyzer preflight   # PostgreSQL, Ollama model, GPU
python -m steam_analyzer smoke       # small live check of Steam reviews, release events and comments
python -m steam_analyzer catalog     # refresh the release catalog and list every release with its window
pytest                               # database tests use synthetic data and clean up after themselves
streamlit run steam_analyzer/app.py  # control panel
```

## How updates and feedback windows work

**Which announcements count as releases.** Steam labels each announcement with an event type. Small updates (12), regular updates (13) and major updates (14) are releases. News (28), livestream events (11) and in-game events (35) never are, including posts like "Incoming Patch: 27th May" or "PATCH 1.000.400 explained". Every release is its own entry, hotfixes included; there are no nested groups.

**Duplicate posts.** Developers sometimes post twice about one release, for example "Devoid of Liberty: 7.0.0" and "Update Out Now" three hours later. Posts within 12 hours of each other are merged when one has no version number or both have the same one. Both links are kept. Version numbers alone are not trusted, because real titles reuse or mistype them months apart.

**Release time.** Steam publishes no separate "went live" time, so a release's time is its earliest announcement post, labeled `Announcement post` as a fallback. A known correction goes in `config/release_overrides.json` and is labeled `Manual correction`:

```json
[
  {"event_gid": "<Steam event gid>", "released_at": "2026-06-16T11:15:00Z", "reason": "patch went live after the post"},
  {"event_gid": "<Steam event gid>", "is_release": false, "reason": "teaser, not a release"}
]
```

**Windows.** Feedback belongs to an update when `release time <= created_at < next release time`, using exact UTC timestamps. The next release is found in the full catalog whether or not it is selected, so an unselected hotfix still ends the update before it. Feedback posted at the exact second of a release belongs to the newer update. The latest update runs until the run's cutoff and is marked ongoing.

**Honest uncertainty.** A window is marked `uncertain` instead of guessed when:
- the catalog could not be re-checked through the cutoff, so a newer release might exist (applies to the latest update)
- any release has no timestamp
- two distinct releases share the exact same timestamp; both are flagged and share the next boundary

A refresh is only `complete` when paging reaches the oldest announcement. Its count is cross-checked against the news API's announcement count.

**Frozen runs.** Run analysis captures one cutoff, refreshes the catalog, then stores the cutoff, the full timeline and each selected update's window. A release published later does not change a run that already exists.

**Patch-note edits.** Patch notes are stored as immutable snapshots. If Steam shows the notes were edited after the update's window closed, the run uses an earlier captured snapshot when one exists; otherwise it marks the notes `edited_after_window`, and conclusions that depend on them are provisional.

## Optional: read-only database access over MCP

[Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) lets an MCP-compatible tool inspect the tables without being able to change them. Two layers enforce this: a SELECT-only database role, and the server's `restricted` (read-only transaction) mode. The pipeline itself does not use MCP.

```sql
-- as postgres, connected to steam_analyzer
CREATE ROLE steam_reader WITH LOGIN PASSWORD '<choose one>';
GRANT CONNECT ON DATABASE steam_analyzer TO steam_reader;
GRANT USAGE ON SCHEMA public TO steam_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO steam_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO steam_reader;
```

Server entry for your MCP client's config:

```json
{
  "command": "uvx",
  "args": ["--overrides", "<full path>/tools/postgres-mcp-overrides.txt",
           "--from", "postgres-mcp==0.3.0", "postgres-mcp", "--access-mode=restricted"],
  "env": { "DATABASE_URI": "postgresql://steam_reader:<password>@localhost:5432/steam_analyzer" }
}
```

Put it in your client's user-level config so the password stays out of the repo. The overrides file works around two packaging problems in postgres-mcp 0.3.0, explained in its comments.

## Roadmap

- [x] Live checks of the Steam review, event and comment sources
- [x] Update catalog with release-to-release feedback windows
- [ ] Review and update-page comment collectors
- [ ] Caching, run progress, cancellation and resume
- [ ] Local LLM feature and sentiment extraction
- [ ] SQL aggregation, reception labels and reporting views
- [ ] Power BI report
- [ ] Human-labeled validation set

## License

MIT
