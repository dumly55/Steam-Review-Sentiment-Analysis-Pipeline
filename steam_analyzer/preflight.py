"""Checks that local services are ready before a pipeline run."""

import subprocess
from dataclasses import dataclass

import requests

from steam_analyzer.config import Settings


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_postgres(settings: Settings) -> CheckResult:
    try:
        import psycopg

        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
        return CheckResult("PostgreSQL", True, f"connected, server {version}")
    except Exception as exc:
        return CheckResult("PostgreSQL", False, f"{exc}. Check DATABASE_URL in .env and that the service is running.")


def check_ollama(settings: Settings) -> CheckResult:
    try:
        tags = requests.get(f"{settings.ollama_host}/api/tags", timeout=5).json()
    except requests.RequestException as exc:
        return CheckResult("Ollama", False, f"not reachable at {settings.ollama_host} ({exc}). Start the Ollama app.")
    names = {m["name"] for m in tags.get("models", [])}
    if settings.ollama_model not in names:
        return CheckResult("Ollama", False, f"model {settings.ollama_model} missing. Run: ollama pull {settings.ollama_model}")
    return CheckResult("Ollama", True, f"model {settings.ollama_model} available")


def check_gpu() -> CheckResult:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("GPU", False, f"nvidia-smi unavailable ({exc}); Ollama will fall back to CPU")
    return CheckResult("GPU", True, out)


def run_preflight(settings: Settings) -> list[CheckResult]:
    return [check_postgres(settings), check_ollama(settings), check_gpu()]
