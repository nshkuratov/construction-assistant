"""FakeEngine вызывает настоящие обработчики MCP; сессия должна закончиться как после настоящего хода движка."""

import pytest

from src.engine.fake_engine import FakeEngine


@pytest.fixture
def engine(chat):
    return FakeEngine(data_dir=str(chat.dir.parent.parent))


async def test_greeting_gets_the_report_form(engine):
    outcome = await engine.handle(42, "привет")
    assert outcome.outcome == "form"
    assert "Пришлите отчёт" in outcome.message


async def test_clear_item_becomes_a_draft_line(engine, chat):
    outcome = await engine.handle(42, "Сегодня проложили медь 6-ку 15 м")
    assert outcome.outcome in ("draft", "question")
    draft = chat.load_session().draft
    assert draft is not None and draft.lines
    assert draft.lines[0].value == 15.0
    assert draft.day_source == "default"


async def test_named_yesterday_sets_the_day(engine, chat):
    await engine.handle(42, "вчера кабель 3 на 2,5 — 20 м")
    draft = chat.load_session().draft
    assert draft is not None
    assert draft.day_source == "message"
    assert draft.day.isoformat() == "2026-09-01"


async def test_remaining_question_is_an_answer(engine):
    outcome = await engine.handle(42, "сколько осталось по кабелю 3х2,5?")
    assert outcome.outcome == "answer"
    assert outcome.message


async def test_unknown_work_asks_instead_of_guessing(engine, chat):
    outcome = await engine.handle(42, "звездолёт 5 шт")
    assert outcome.outcome == "question"
    assert chat.load_session().draft is None


async def test_chat_dir_is_restored_after_a_turn(engine):
    from src.config import Config

    before = Config.CA_CHAT_DIR
    await engine.handle(42, "привет")
    assert Config.CA_CHAT_DIR == before
