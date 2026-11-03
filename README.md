# Steam Review Sentiment Analysis Pipeline

**LLM-powered player feedback analytics for Steam game updates.** Python ETL · PostgreSQL · SQL · local NLP with Ollama · Streamlit · Power BI

> **Status: Phase 1 complete (setup and source checks).** The three Steam sources have been verified live, and the local database, model and GPU pass preflight. Nothing is collected or analyzed yet and there are no findings. See [docs/source_findings.md](docs/source_findings.md) for what was verified.

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
steam_analyzer/   Python package: config, HTTP retries, Steam sources, preflight, smoke check, Streamlit app
sql/              PostgreSQL schema and, later, reporting views
tests/            pytest tests (fixtures are labeled synthetic)
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
pytest
streamlit run steam_analyzer/app.py
```

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

- [ ] Live Steam review and update-page comment collectors
- [ ] Update catalog with release-to-release feedback windows
- [ ] PostgreSQL schema, caching and run tracking
- [ ] Local LLM feature and sentiment extraction
- [ ] SQL aggregation, reception labels and reporting views
- [ ] Power BI report
- [ ] Human-labeled validation set

## License

MIT
