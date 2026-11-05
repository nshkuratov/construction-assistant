"""Рабочее хранилище чата в ``<data dir>/chats/<chat_id>/``.

Файлы: ``session.json`` (``ChatSession``) и ``snapshots/<site_id>.json`` (``CatalogSnapshot``).
Каждая запись атомарна (временный файл + замена). Каждый цикл чтение-изменение-запись ``session.json``
держит файловую блокировку, поэтому процесс бота и процесс MCP никогда не перемешиваются. Блокировка —
``fcntl`` на POSIX и ``msvcrt`` на Windows.
"""

import contextlib
import re
import secrets
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from src.config import Config
from src.models import CatalogSnapshot, ChatSession
from src.utils.persistence import atomic_write_json, safe_load_json

DRAFT_ID_LEN = 8
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

if sys.platform == "win32":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def new_draft_id() -> str:
    """Короткий id для callback-данных (Telegram разрешает 64 байта на кнопку)."""
    return secrets.token_hex(DRAFT_ID_LEN // 2)


def new_turn_id() -> str:
    return secrets.token_hex(6)


class SessionStore:
    def __init__(self, chat_id: int, data_dir: str | None = None, history_turns: int | None = None):
        self.chat_id = chat_id
        self.dir = Path(data_dir if data_dir is not None else Config.CA_DATA_DIR) / "chats" / str(chat_id)
        self.history_turns = Config.HISTORY_TURNS if history_turns is None else history_turns

    @classmethod
    def from_chat_dir(cls, chat_dir: str, history_turns: int | None = None) -> "SessionStore":
        """Открывает хранилище по точному каталогу чата (процесс MCP получает ``CA_CHAT_DIR``)."""
        path = Path(chat_dir)
        store = cls(int(path.name), str(path.parent.parent), history_turns)
        store.dir = path
        return store

    @property
    def session_path(self) -> Path:
        return self.dir / "session.json"

    def snapshot_path(self, site_id: str) -> Path:
        if not _SAFE_ID.fullmatch(site_id):
            raise ValueError(f"bad site id {site_id!r}")
        return self.dir / "snapshots" / f"{site_id}.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "session.lock", "a+b") as fh:
            _lock(fh)
            try:
                yield
            finally:
                _unlock(fh)

    def _read_session(self) -> ChatSession:
        raw = safe_load_json(str(self.session_path), default=None)
        return ChatSession.model_validate(raw) if raw else ChatSession(chat_id=self.chat_id)

    def _write_session(self, session: ChatSession) -> None:
        session.history = session.history[-self.history_turns :] if self.history_turns > 0 else []
        atomic_write_json(str(self.session_path), session.model_dump(mode="json"), indent=2)

    def load_session(self) -> ChatSession:
        with self._locked():
            return self._read_session()

    def save_session(self, session: ChatSession) -> None:
        with self._locked():
            self._write_session(session)

    def update_session(self, mutate: Callable[[ChatSession], None]) -> ChatSession:
        """Читает, применяет ``mutate`` на месте, записывает — всё под блокировкой. Возвращает сохранённую сессию."""
        with self._locked():
            session = self._read_session()
            mutate(session)
            self._write_session(session)
            return session

    def begin_turn(self) -> str:
        """Начинает ход движка: новый ``turn_id``, прежний исход очищен."""

        def _start(s: ChatSession) -> None:
            s.turn_id = new_turn_id()
            s.turn_outcome = None

        turn_id = self.update_session(_start).turn_id
        assert turn_id is not None
        return turn_id

    def write_snapshot(self, snapshot: CatalogSnapshot) -> None:
        path = self.snapshot_path(snapshot.site_id)
        atomic_write_json(str(path), snapshot.model_dump(mode="json"))

    def read_snapshot(self, site_id: str) -> CatalogSnapshot | None:
        raw = safe_load_json(str(self.snapshot_path(site_id)), default=None)
        return CatalogSnapshot.model_validate(raw) if raw else None


def chat_dir_from_env() -> str | None:
    """Каталог чата, с которым работает процесс MCP, или ``None``, если он не задан."""
    return Config.CA_CHAT_DIR or None
