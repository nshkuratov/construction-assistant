"""`CA_SKIP_DOTENV` не даёт `src.config` загрузить `.env` (тесты и процесс MCP не должны видеть секреты)."""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = "from src.config import Config; print(repr(Config.TELEGRAM_BOT_TOKEN))"


def _token_seen(cwd: Path, skip: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN", "CA_SKIP_DOTENV")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if skip is not None:
        env["CA_SKIP_DOTENV"] = skip
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_dotenv_is_loaded_by_default(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=from-dotenv\n", encoding="utf-8")
    assert _token_seen(tmp_path, None) == "'from-dotenv'"


def test_skip_flag_stops_dotenv(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=from-dotenv\n", encoding="utf-8")
    assert _token_seen(tmp_path, "1") == "''"


def test_test_suite_runs_with_the_skip_flag():
    assert os.environ.get("CA_SKIP_DOTENV") == "1"
