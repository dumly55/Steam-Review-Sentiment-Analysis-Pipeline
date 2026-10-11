# Steam Review Sentiment Analysis Pipeline

**LLM-powered player feedback analytics for Steam game updates.** Python ETL · PostgreSQL · SQL · local NLP with Ollama · Streamlit · Power BI

> **Status: v0 scaffold.** This is only the project skeleton. Nothing collects data, runs AI, or produces findings yet.

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
steam_analyzer/   Python package (config, app; collectors and pipeline to come)
sql/              PostgreSQL schema and, later, reporting views
tests/            pytest tests
```

## Setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit values
pytest
streamlit run steam_analyzer/app.py
```

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
