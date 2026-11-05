"""Жизненный цикл BaseEngine через поддельный ``codex`` (см. tests/fake_codex.py)."""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from src.config import Config
from src.engine.codex_engine import CodexEngine

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake codex is a POSIX script")

FAKE = Path(__file__).parent / "fake_codex.py"


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """Ставит подделку как ``codex`` в PATH. Возвращает функцию, которая задаёт её режим."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(Config, "CODEX_COMMAND", "codex")
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "test-key")

    def set_mode(mode: str) -> Path:
        (bin_dir / "mode").write_text(mode)
        return bin_dir

    return set_mode


def _engine(chat) -> CodexEngine:
    return CodexEngine(data_dir=str(chat.dir.parent.parent), log_path=str(chat.dir.parent / "engine.jsonl"))


async def test_normal_turn_returns_the_stored_outcome(chat, fake_codex):
    bin_dir = fake_codex("normal")
    outcome = await _engine(chat).handle(42, "привет")
    assert (outcome.outcome, outcome.message) == ("answer", "Готово.")
    record = json.loads((bin_dir / "record.json").read_text())
    assert record["agents_md"] is True
    assert Path(record["cwd"]).resolve().parent != Path(__file__).resolve().parents[1]


async def test_turn_without_finish_becomes_answer_with_friction_and_restores_session(chat, fake_codex):
    fake_codex("no_finish")
    outcome = await _engine(chat).handle(42, "что-то")
    assert (outcome.outcome, outcome.message) == ("answer", "Просто текст без вызова finish_turn")
    assert chat.load_session().site_id == "sad"
    friction = Path(Config.FRICTION_LOG).read_text(encoding="utf-8")
    assert "finish_turn" in friction


async def test_crash_retries_once_then_errors_and_restores_session(chat, fake_codex):
    bin_dir = fake_codex("crash")
    outcome = await _engine(chat).handle(42, "отчёт")
    assert outcome.outcome == "error"
    assert (bin_dir / "runs").read_text() == "xx"
    assert chat.load_session().site_id == "sad"


async def test_crash_then_success_uses_a_new_turn_id(chat, fake_codex):
    fake_codex("crash_once")
    outcome = await _engine(chat).handle(42, "отчёт")
    assert (outcome.outcome, outcome.message) == ("answer", "Со второго раза.")
    assert chat.load_session().site_id == "sad"


async def test_finished_turn_never_retries_even_on_nonzero_exit(chat, fake_codex):
    fake_codex("finish_then_crash")
    outcome = await _engine(chat).handle(42, "отчёт")
    assert (outcome.outcome, outcome.message) == ("answer", "Ответ.")


async def test_hang_times_out_once_kills_the_tree_and_does_not_retry(chat, fake_codex, monkeypatch):
    monkeypatch.setattr(Config, "ENGINE_TURN_TIMEOUT_S", 2)
    bin_dir = fake_codex("hang")
    started = time.monotonic()
    outcome = await _engine(chat).handle(42, "отчёт")
    elapsed = time.monotonic() - started
    assert outcome.outcome == "error"
    assert elapsed < 2 * 2 + 5, "a timeout must not be retried"
    pid = int((bin_dir / "child.pid").read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert chat.load_session().site_id == "sad"


async def test_missing_executable_gives_error(chat, monkeypatch):
    monkeypatch.setattr(Config, "CODEX_COMMAND", "definitely-not-a-codex-binary")
    outcome = await _engine(chat).handle(42, "отчёт")
    assert outcome.outcome == "error"


async def test_telemetry_line_has_no_message_text_or_secret(chat, fake_codex):
    fake_codex("normal")
    await _engine(chat).handle(42, "секретный текст прораба")
    log = (chat.dir.parent / "engine.jsonl").read_text(encoding="utf-8")
    row = json.loads(log.splitlines()[-1])
    assert set(row) == {"ts", "backend", "duration_s", "exit_code", "retries", "outcome"}
    assert "секретный" not in log and "test-key" not in log
    assert row["backend"] == "codex" and row["exit_code"] == 0
