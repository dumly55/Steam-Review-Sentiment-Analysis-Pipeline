import json
import os
from dataclasses import dataclass
from pathlib import Path

ANALYSIS_FILE = Path(__file__).resolve().parent.parent / "config" / "analysis.json"


@dataclass(frozen=True)
class Settings:
    steam_app_id: int
    database_url: str
    ollama_host: str
    ollama_model: str
    log_level: str = "INFO"


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    return Settings(
        steam_app_id=int(os.getenv("STEAM_APP_ID", "553850")),
        database_url=os.getenv(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/steam_analyzer"
        ),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def analysis_config(settings: Settings, max_items: int | None = None, path: Path = ANALYSIS_FILE) -> dict:
    """The analysis settings a new run freezes: config/analysis.json plus the model name and any item cap."""
    config = json.loads(path.read_text(encoding="utf-8"))
    config["model"] = settings.ollama_model
    if max_items is not None:
        config["max_items_per_source"] = max_items
    return config
