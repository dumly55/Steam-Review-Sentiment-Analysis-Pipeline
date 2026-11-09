# Steam Review Sentiment Analysis Pipeline

**LLM-powered player feedback analytics for Steam game updates.** Python ETL · PostgreSQL · SQL · local NLP with Ollama · Streamlit · Power BI

> **Status: Phase 3 (one update end to end) works on live data.** A run collects Steam reviews and announcement comments for each selected update's window, analyzes them with a local model, and computes source and overall reception labels in SQL. The control panel shows a results preview. Live runs so far were capped samples used to verify the pipeline, so there are **no published findings yet**, and model accuracy has **not been validated** against human labels. See [docs/source_findings.md](docs/source_findings.md) for what was verified against live Steam data.

## What it does

Pick one or more Steam game updates (default game: Helldivers 2, app ID 553850). For each update, the pipeline:

1. **Extracts**: collects Steam reviews and update-page comments posted while that update was the current one, up to the next release.
2. **Transforms**: uses a local LLM (Ollama) to pull out which features players praised or criticized, with exact quoted evidence.
3. **Loads and aggregates**: stores everything in PostgreSQL, where SQL counts distinct authors per feature and assigns reception labels using fixed rules.
4. **Visualizes**: shows a preview in the control panel. The Power BI dashboard is next on the roadmap.

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
  runs.py           run creation: frozen cutoff, timeline, windows, patch notes and config
  steam_sources.py  Steam reviews, events and comment-page access
  collectors.py     review and comment collectors with per-page checkpoints
  feedback.py       which feedback counts: window, historical edits, language (pure functions)
  patch_notes.py    patch notes to plain text with a [C#] tag per change (pure functions)
  extraction.py     prompt, JSON schema, validation, cache key and the Ollama client
  pipeline.py       processing a run: collect, check eligibility, analyze, aggregate
  results.py        reads results from the SQL views
  demo.py           the synthetic demo game (invented data)
  app.py            Streamlit control panel
sql/              schema.sql (tables) and views.sql (all counting and labels)
config/           analysis.json (thresholds, model settings, feature vocabulary) and release_overrides.json
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

For about 30% faster analysis on a 10 GB GPU, let Ollama answer two requests at once (measured in [docs/source_findings.md](docs/source_findings.md#throughput-tuning)), then quit and reopen Ollama:

```powershell
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_NUM_PARALLEL 2
setx OLLAMA_KV_CACHE_TYPE q8_0
```

The pipeline sends `concurrent_requests` (default 2, in `config/analysis.json`) at once. Without these settings Ollama queues the second request, which is just as fast as sending one at a time.

## Commands

```powershell
python -m steam_analyzer initdb      # create the database if missing and apply sql/schema.sql and sql/views.sql (safe to rerun)
python -m steam_analyzer preflight   # PostgreSQL, Ollama model, GPU
python -m steam_analyzer smoke       # small live check of Steam reviews, release events and comments
python -m steam_analyzer catalog     # refresh the release catalog and list every release with its window and id
python -m steam_analyzer run 94      # analyze update #94 (ids from `catalog`); add --max-items 50 for a quick capped test
python -m steam_analyzer cancel 12   # ask run 12 to stop after the items it is working on
python -m steam_analyzer process 12  # resume run 12 after a cancel, Ctrl+C or a failure; finished work is kept
python -m steam_analyzer results 12  # print run 12's labels, counts and leading reasons
python -m steam_analyzer demo        # run the SYNTHETIC demo end to end in seconds (no Steam or Ollama needed)
pytest                               # database tests use synthetic data and clean up after themselves
streamlit run steam_analyzer/app.py  # control panel
```

A full window is large: a typical Helldivers 2 update has 1,000–3,000 English reviews plus hundreds of comments, and `qwen3:8b` on an RTX 3080 takes roughly 1–3 seconds per item once loaded, so one update can take about an hour (less with the two-request Ollama settings in Setup). Run analysis in the control panel starts a background worker, and the run panel refreshes every few seconds with a progress table for each selected update: collection status, collected, excluded, eligible, cache hits, analyzed, failed and pending. The log is in `data/logs/run_<id>.log`.

**Cancel and resume.** Cancel (in the panel or with the `cancel` command) stops the worker at its next check: between collected sources, and before each analysis result is stored. The one or two items already being analyzed finish and are discarded, so a cancel takes a few seconds. The run is marked `cancelled`. Everything stored so far is kept: collected pages, eligibility decisions and every stored analysis. Resume starts from the checkpoints and only analyzes items that are still pending, so nothing is collected or analyzed twice. Ctrl+C in a terminal does the same as Cancel.

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

## How feedback is collected

Two independent collectors read each run's frozen window.

- **Steam reviews**: every English review created in the window, from `IUserReviewsService/GetAppReviews` sorted by Recent with off-topic (review-bomb) activity included and all purchase types. Steam's date filter is used, and every timestamp is checked again locally.
- **Announcement comments**: the public comment thread of each announcement post about the release (a release with two posts has two threads), read page by page, oldest first. The whole thread is read so its reported total can be checked; comments posted after the window are then excluded, not reassigned to the next update.

Each page is stored together with a checkpoint, so stopping and resuming never loses or duplicates work. Each source ends with a status:

| Status | Meaning |
|---|---|
| `complete` | Paging ended and the number collected reached Steam's own reported total. |
| `partial` | Some pages were read, but a request failed, a cap or page limit was hit, or fewer items than reported were found. |
| `unavailable` | Nothing could be read, or the announcement has no comment thread. |

Only `complete` sources can receive a reception label. A blocked or unreadable page is never treated as "zero comments".

## Which feedback counts

Each item keeps its identity (source, Steam ID, author, original creation time, link) and every distinct version of its text that has been seen. Before any AI runs, deterministic checks decide eligibility, in this order:

1. **Window**: created at or after the release and before the next release (or the cutoff).
2. **Historical edits**: Steam shows when a review or comment was last edited. The run uses the latest captured version that was last edited *before* the window ended. If every captured version was edited later, the current text cannot stand in for what was written during the window, so the item is excluded as `edited_after_window` and counted.
3. **Language**: English only. Reviews use Steam's language tag. Comments have none, so a small word-list check excludes text that is clearly another language; short or ambiguous text stays in and is counted as "uncertain language".
4. **Text**: empty text is excluded.

## Local AI extraction

Each eligible item is sent to the local model (default `qwen3:8b` via Ollama) with the update's patch notes, where every listed change is tagged `[C1]`, `[C2]`, and so on. The model returns JSON with the overall text sentiment, how the item relates to this update, and zero or more feature observations, each with a feature key, category, sentiment, relevance, an exact quote as evidence, and the patch change it refers to.

- Patch notes and feedback are treated as untrusted data, never as instructions.
- An answer is rejected if it breaks the schema, names the wrong item, cites a change that does not exist, or quotes evidence that does not appear in the feedback (whitespace differences are ignored). A rejected answer is retried with the reason, up to the configured number of attempts; after that the item is recorded as **failed** and stays in the coverage denominator.
- Requests use a pinned seed and temperature 0, but local models are still not fully deterministic, so every result is cached rather than recomputed.
- Long feedback and very long patch notes are cut to fit the model's input, and the cut is recorded and shown.
- Passing validation means the answer is well formed and grounded in the text. It does not mean the interpretation is correct. **Accuracy has not been measured yet**; a human-labeled validation set is on the roadmap.

**Cache.** Assessments are reused across runs. The cache key covers the item, the exact text, the patch-note context and instructions, the model name and digest, the prompt and schema versions, and the inference settings. Changing any of those re-analyzes the affected items. Changing a label threshold does not need new inference: the SQL views simply recompute.

## How counting and labels work

All counts, shares and labels are computed in SQL (`sql/views.sql`), never by the model. Thresholds are frozen into each run's configuration, so editing `config/analysis.json` never changes an existing run.

- **Distinct authors, not posts.** Within an update, source and feature, each identifiable author counts once. Ten comments from one person are one person. If an author is both positive and negative about a feature, or explicitly mixed, they count once as mixed.
- **Sources are never added together.** Reviews and comments are reported separately because the same person may appear in both.
- **Unknown authors** (comments where Steam shows no profile) can supply excerpts but are never counted as distinct people.
- **`analyzed_authors`**: identifiable authors with at least one successfully analyzed eligible item in that source and window. It is the denominator for every feature share, shown next to each percentage, for example "24 of 120 analyzed authors (20%)". Shares across features need not add up to 100%.
- **Patch-related** observations are those the model tied to this update, or that may be related and are linked to a listed change. Other observations appear as tentative, general or unrelated topics and never decide the verdict.
- **`qualifying_update_authors`** (N): authors whose patch-related observations include a clear opinion, each reduced to positive, negative or mixed. Someone who praises the game but criticizes a patch change is a *negative* patch reaction. Neutral or unclear observations never dilute a clear opinion.
- **Recurring**: a feature is recurring praise or criticism when at least 5 authors and at least 5% of analyzed authors hold that opinion; a popular but divided feature is "recurring, divided". Everything else is shown as limited or isolated feedback, not hidden.

Source label, checked in this order (defaults from `config/analysis.json`):

| Label | Rule |
|---|---|
| Insufficient feedback | Collection not complete, analysis coverage below 90%, or fewer than 20 qualifying authors. |
| Positive | Positive authors / N ≥ 60% and (positive − negative) / N ≥ 20%. |
| Negative | Negative authors / N ≥ 60% and (negative − positive) / N ≥ 20%. |
| Mixed | Enough evidence, but neither rule above passes. |

**Overall label.** If both sources are usable and agree, that label. If they disagree, Mixed. If only one is usable, its label marked "provisional, reviews only" or "provisional, comments only". If neither, Insufficient feedback. An update with an uncertain window gets "Uncertain window" instead of a verdict.

These labels describe reception **among the analyzed feedback authors**. They are screening rules, not statistical significance, and feedback posted during an update's lifetime is not proof that the update caused it. Steam's recommendation share and the overall text sentiment are shown as separate measures and never decide the patch verdict.

## Synthetic demo

`python -m steam_analyzer demo`, or the **Synthetic demo data** switch in the control panel, loads an invented game with three releases, a sale post, reviews, comment threads and prepared "model" answers, then runs the real pipeline on it. It is stored under a separate fake app ID, marked synthetic, and labeled on every screen. It shows each rule at work: a late comment, an eight-post commenter counted once, edits after the window, a non-English comment, a failed answer kept in the denominator, and two sources that disagree. None of it is a finding.

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
- [x] Review and update-page comment collectors with checkpoints
- [x] Local LLM feature and sentiment extraction with validation and a durable cache
- [x] SQL aggregation and reception labels, with a results preview and a synthetic demo
- [x] Multi-update runs: progress by update and source, cancellation and resume from the control panel
- [ ] Power BI reporting views and report
- [ ] Human-labeled validation set and measured model accuracy

## License

MIT
