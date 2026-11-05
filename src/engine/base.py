"""Общая механика движка для всех бэкендов: одно сообщение прораба на входе, один ``EngineOutcome`` на выходе.

Бэкенд наследует ``BaseEngine`` и реализует только ``run_harness``. Базовый класс ведёт жизненный
цикл хода: начинает ход, запускает харнесс с ограничением по времени, читает исход, который харнесс
сохранил через ``finish_turn``, восстанавливает сессию, если ход не завершился, и пишет одну строку
телеметрии. Исход всегда читается из сессии до проверки кода завершения.
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.config import Config
from src.models import ChatSession, EngineOutcome
from src.session.store import SessionStore
from src.utils.persistence import append_jsonl
from src.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)

ENGINE_LOG = "logs/engine.jsonl"
ERROR_TEXT = "Не получилось обработать сообщение. Попробуйте ещё раз чуть позже."
TIMEOUT_TEXT = "Обработка заняла слишком много времени. Попробуйте ещё раз или напишите короче."
FALLBACK_MAX_CHARS = 600

_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()


def _chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks.setdefault(chat_id, threading.Lock())


@dataclass
class HarnessResult:
    exit_code: int | None
    final_message: str = ""
    timed_out: bool = False
    os_error: bool = False


@dataclass
class TurnContext:
    store: SessionStore
    turn_id: str
    text: str


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Убивает харнесс и все его дочерние процессы. POSIX: группа процессов. Windows: ``taskkill /T``."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def run_process(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_s: float,
    stdout_path: Path,
    stderr_path: Path | None = None,
) -> tuple[int | None, bool]:
    """Запускает ``cmd`` с ограничением по времени. Возвращает ``(exit_code, timed_out)``.

    Вывод идёт в файлы, чтобы разговорчивый харнесс не мог заблокироваться на полном канале. При
    таймауте ``exit_code`` равен ``None``. Бросает ``OSError``, если исполняемый файл не запустился.
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(stdout_path, "wb") as out, open(stderr_path or os.devnull, "wb") as err:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **kwargs
        )
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return None, True
        finally:
            # Осиротевший дочерний MCP-процесс может пережить чистый выход; убийство группы дёшево и безопасно.
            if sys.platform != "win32":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)


class BaseEngine:
    backend = "base"

    def __init__(self, data_dir: str | None = None, log_path: str = ENGINE_LOG):
        self.data_dir = data_dir
        self.log_path = log_path

    def run_harness(self, ctx: TurnContext) -> HarnessResult:
        raise NotImplementedError

    async def handle(self, chat_id: int, text: str) -> EngineOutcome:
        return await asyncio.to_thread(self._handle_sync, chat_id, text)

    def _handle_sync(self, chat_id: int, text: str) -> EngineOutcome:
        with _chat_lock(chat_id):
            store = SessionStore(chat_id, self.data_dir)
            before = store.load_session()
            started = time.monotonic()
            retries = 0
            exit_code: int | None = None
            outcome: EngineOutcome | None = None
            while outcome is None:
                ctx = TurnContext(store, store.begin_turn(), text)
                try:
                    result = self.run_harness(ctx)
                except OSError as e:
                    logger.error("engine %s could not start: %s", self.backend, e)
                    result = HarnessResult(exit_code=None, os_error=True)
                exit_code = result.exit_code
                outcome = store.load_session().turn_outcome
                if outcome is not None:
                    break
                if result.timed_out:
                    outcome = EngineOutcome(outcome="error", message=TIMEOUT_TEXT)
                elif result.exit_code == 0 and result.final_message.strip():
                    outcome = self._fallback_answer(store, ctx, result.final_message)
                elif retries < Config.ENGINE_RETRIES:
                    retries += 1
                    self._restore(store, before)
                    continue
                else:
                    outcome = EngineOutcome(outcome="error", message=ERROR_TEXT)
            if store.load_session().turn_outcome is None:
                self._restore(store, before, outcome=outcome)
            self._telemetry(started, exit_code, retries, outcome)
            return outcome

    @staticmethod
    def _restore(store: SessionStore, before: ChatSession, outcome: EngineOutcome | None = None) -> None:
        """Возвращает сессию в состояние до хода, сохраняя ``outcome`` как результат хода."""
        restored = before.model_copy(deep=True)
        restored.turn_outcome = outcome
        store.save_session(restored)

    def _fallback_answer(self, store: SessionStore, ctx: TurnContext, message: str) -> EngineOutcome:
        append_jsonl(
            Config.FRICTION_LOG,
            {
                "ts": utcnow_iso(),
                "chat_id": store.chat_id,
                "turn_id": ctx.turn_id,
                "summary": "harness ended without finish_turn",
                "details": None,
            },
        )
        return EngineOutcome(outcome="answer", message=message.strip()[:FALLBACK_MAX_CHARS])

    def _telemetry(self, started: float, exit_code: int | None, retries: int, outcome: EngineOutcome) -> None:
        if not Config.ENABLE_RUN_TELEMETRY:
            return
        append_jsonl(
            self.log_path,
            {
                "ts": utcnow_iso(),
                "backend": self.backend,
                "duration_s": round(time.monotonic() - started, 2),
                "exit_code": exit_code,
                "retries": retries,
                "outcome": outcome.outcome,
            },
        )
