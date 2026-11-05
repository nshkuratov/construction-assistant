"""Фабрика движков: бэкенд выбирается через ``Config.ENGINE_BACKEND``."""

from src.config import Config
from src.interfaces.engine import Engine


def make_engine() -> Engine:
    backend = Config.ENGINE_BACKEND.strip().lower()
    if backend == "codex":
        from src.engine.codex_engine import CodexEngine

        return CodexEngine()
    raise ValueError(f"Unknown ENGINE_BACKEND {Config.ENGINE_BACKEND!r}; use 'codex'")
