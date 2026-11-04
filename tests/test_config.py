import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import _CONFIG_ENV_VARS

ROOT = Path(__file__).resolve().parent.parent
_PROBE = (
    "import dotenv, json; dotenv.load_dotenv = lambda *a, **k: None\n"
    "from src.config import Config\n"
    "print(json.dumps({k: v for k, v in vars(Config).items() if k.isupper()}))"
)


def _config(**env) -> dict:
    clean = {k: v for k, v in os.environ.items() if k not in _CONFIG_ENV_VARS}
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=ROOT, env={**clean, **env}, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_defaults():
    c = _config()
    assert (c["ENGINE_BACKEND"], c["ENGINE_TURN_TIMEOUT_S"], c["ENGINE_RETRIES"]) == ("codex", 180, 1)
    assert (c["SHEETS_BACKEND"], c["CRM_BACKEND"], c["CA_TZ"]) == ("fixture", "fake", "Europe/Moscow")
    assert (c["HISTORY_TURNS"], c["SNAPSHOT_MAX_AGE_S"], c["CARD_PAGE_SIZE"]) == (6, 60, 8)
    assert c["TELEGRAM_ALLOWED_CHAT_IDS"] == ""  # пусто = запретить всем


def test_env_override_and_bad_int_fallback():
    c = _config(ENGINE_BACKEND="fake", HISTORY_TURNS="abc")
    assert c["ENGINE_BACKEND"] == "fake" and c["HISTORY_TURNS"] == 6


def test_env_example_and_conftest_list_every_config_var():
    names = set(_config())
    assert names == set(_CONFIG_ENV_VARS)
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert [v for v in sorted(names) if f"{v}=" not in text] == []
