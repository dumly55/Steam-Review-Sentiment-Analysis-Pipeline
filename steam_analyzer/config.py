import os
from dataclasses import dataclass


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
