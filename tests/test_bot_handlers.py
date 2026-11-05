"""Тесты обработчиков через ``Application.process_update`` с поддельными апдейтами и записывающим поддельным Bot.

Подменена только сеть Telegram (``FakeBot._post``). Всё остальное настоящее: обработчики, карточка,
сервисы, ``FakeSheetGateway``, ``FakeCrmSink``, хранилище сессий и сценарный ``FakeEngine``
(он вызывает настоящие обработчики инструментов MCP).
"""

import asyncio
import json
from datetime import date

import pytest
from telegram import Bot, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder

from src.bot.app import build_handlers, register
from src.bot.handlers import BUSY_TEXT, CALLBACK_BUSY, REFUSAL, VOICE_HINT
from src.config import Config
from src.engine.fake_engine import FakeEngine
from src.interfaces.fakes import FakeCrmSink, FakeSheetGateway
from src.models import EngineOutcome
from src.session.store import SessionStore

CHAT = 42
OTHER = 77
DAY = date(2026, 9, 2)


class FakeBot(Bot):
    """``telegram.Bot``, чей сетевой вызов записывает запрос, а не отправляет его."""

    def __init__(self):
        super().__init__("123:fake")
        self._requests: list[dict] = []
        self._next_id = 1000

    @property
    def requests(self) -> list[dict]:
        return self._requests

    async def _post(self, endpoint, data=None, **kwargs):
        if endpoint == "getMe":
            return {"id": 123, "is_bot": True, "first_name": "bot", "username": "bot"}
        data = dict(data or {})
        self._next_id += 1
        markup = data.get("reply_markup")
        rows = []
        if isinstance(markup, InlineKeyboardMarkup):
            rows = [[(b.text, b.callback_data, b.url) for b in row] for row in markup.inline_keyboard]
        record = {
            "endpoint": endpoint,
            "chat_id": data.get("chat_id"),
            "text": data.get("text"),
            "rows": rows,
            "message_id": data.get("message_id") or self._next_id,
            "answer": data.get("text"),
        }
        self._requests.append(record)
        if endpoint in ("sendMessage", "editMessageText"):
            return {
                "message_id": record["message_id"],
                "date": 0,
                "text": data.get("text", ""),
                "chat": {"id": data["chat_id"], "type": "private"},
            }
        return True


class Harness:
    def __init__(self, app, bot, handlers, gateway, crm, engine, data_dir):
        self.app, self.bot, self.handlers = app, bot, handlers
        self.gateway, self.crm, self.engine, self.data_dir = gateway, crm, engine, data_dir
        self._update_id = 0

    def store(self, chat_id=CHAT) -> SessionStore:
        return SessionStore(chat_id, self.data_dir)

    def _update(self, chat_id: int, **payload) -> Update:
        self._update_id += 1
        user = payload.get("user") or {"id": chat_id, "is_bot": False, "first_name": "Foreman"}
        chat = {"id": chat_id, "type": "private"}
        body = {"update_id": self._update_id}
        if "callback" in payload:
            body["callback_query"] = {
                "id": str(self._update_id),
                "from": user,
                "chat_instance": "x",
                "data": payload["callback"],
                "message": {"message_id": payload.get("message_id", 1), "date": 0, "chat": chat, "text": "card"},
            }
        else:
            message = {"message_id": self._update_id, "date": 0, "chat": chat, "from": user, **payload["message"]}
            body["message"] = message
        return Update.de_json(body, self.bot)

    async def text(self, text: str, chat_id=CHAT):
        await self.app.process_update(self._update(chat_id, message={"text": text}))

    async def command(self, text: str, chat_id=CHAT):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(self._update(chat_id, message={"text": text, "entities": entities}))

    async def voice(self, chat_id=CHAT):
        voice = {"file_id": "f", "file_unique_id": "u", "duration": 1}
        await self.app.process_update(self._update(chat_id, message={"voice": voice}))

    async def press(self, data: str, chat_id=CHAT, message_id=None):
        mid = message_id or self.store(chat_id).load_session().card_message_id or 1
        await self.app.process_update(self._update(chat_id, callback=data, message_id=mid))

    def sent(self, endpoint="sendMessage", chat_id=CHAT) -> list[dict]:
        return [r for r in self.bot.requests if r["endpoint"] == endpoint and r["chat_id"] == chat_id]

    def last_text(self, chat_id=CHAT) -> str:
        return self.sent(chat_id=chat_id)[-1]["text"]

    def buttons(self, record: dict) -> dict[str, tuple]:
        return {label: (data, url) for row in record["rows"] for label, data, url in row}

    def card_button(self, label_part: str, chat_id=CHAT) -> str:
        """Данные callback первой кнопки с ``label_part`` в подписи, в самом новом сообщении с такой кнопкой."""
        for record in reversed(self.bot.requests):
            if record["chat_id"] != chat_id:
                continue
            for label, (data, _) in self.buttons(record).items():
                if label_part in label and data:
                    return data
        raise AssertionError(f"no button with {label_part!r}")

    def cell(self, tab: str, row: int, col: str):
        return self.gateway.cell(tab, row, col)


@pytest.fixture
async def harness(tmp_path, monkeypatch, sample_grid):
    for module in ("catalog_tools", "session_tools"):
        monkeypatch.setattr(f"src.mcp.wrappers.{module}.today", lambda: DAY)
    monkeypatch.setattr("src.bot.handlers.today_in_tz", lambda: DAY)
    monkeypatch.setattr(Config, "FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    gateway = FakeSheetGateway(sample_grid, ["Садик", "Школа"])
    crm = FakeCrmSink()
    data_dir = str(tmp_path)
    engine = FakeEngine(data_dir=data_dir)
    handlers = build_handlers(
        engine=engine, gateway=gateway, crm=crm, data_dir=data_dir, allowed_chat_ids=frozenset({CHAT, OTHER})
    )
    bot = FakeBot()
    app = ApplicationBuilder().bot(bot).concurrent_updates(True).build()
    register(app, handlers)
    await app.initialize()
    yield Harness(app, bot, handlers, gateway, crm, engine, data_dir)
    await app.shutdown()


async def pick_site(h: Harness, label="Садик", chat_id=CHAT):
    await h.text("привет", chat_id)
    await h.press(h.card_button(label, chat_id), chat_id)


async def test_unauthorized_chat_is_refused(harness):
    await harness.text("медь 6-ка 15 м", chat_id=999)
    assert harness.last_text(999) == REFUSAL.format(chat_id=999)
    assert not harness.store(999).dir.exists()


async def test_first_contact_shows_the_site_picker_not_the_engine(harness):
    await harness.text("медь 6-ка 15 м")
    record = harness.sent()[-1]
    assert "Выберите объект" in record["text"]
    assert {"Садик", "Школа"} <= set(harness.buttons(record))
    assert harness.store().load_session().history == []


async def test_site_pick_then_help_and_commands(harness):
    await harness.command("/start")
    assert any("Напишите, что сделано" in r["text"] for r in harness.sent())
    await harness.command("/help")
    await pick_site(harness)
    assert harness.store().load_session().site_id is not None
    await harness.command("/sheet")
    last = harness.sent()[-1]
    assert harness.buttons(last)["📊 Открыть таблицу"][1] == "https://fake.sheets/Садик"


async def test_voice_note_gets_the_dictation_hint(harness):
    await harness.voice()
    assert harness.last_text() == VOICE_HINT
    assert [r["endpoint"] for r in harness.bot.requests] == ["sendMessage"]


async def test_happy_path_message_card_confirm_both_sinks_and_link(harness):
    await pick_site(harness)
    await harness.text("Сегодня проложили медь 6-ку 15 м")
    assert not harness.crm.entries
    before = harness.cell("Садик", 17, "T")
    card = harness.sent()[-1]
    assert "Записать" in " ".join(harness.buttons(card))
    await harness.press(harness.card_button("Записать"))
    assert harness.cell("Садик", 17, "T") == (before or 0) + 15
    assert len(harness.crm.entries) == 1
    assert next(iter(harness.crm.entries.values())).report_author == "Foreman (Telegram ID: 42)"
    summary = harness.sent()[-1]
    assert summary["text"].startswith("✅ Записано")
    assert harness.buttons(summary)["📊 Открыть таблицу"][1] == "https://fake.sheets/Садик"
    assert harness.store().load_session().draft is None


async def test_nothing_is_written_without_confirm(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    assert harness.crm.entries == {}
    assert not any(c[0] in ("upsert_entries", "delete_entries") for c in harness.crm.calls)
    assert harness.cell("Садик", 17, "T") in (None, "", 0)


async def test_report_author_comes_from_confirming_user_and_survives_crm_retry(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    harness.crm.fail("upsert_entries")
    update = harness._update(
        CHAT, callback=harness.card_button("Записать"),
        user={"id": 1234, "is_bot": False, "first_name": "Иван", "last_name": "Петров", "username": "ivan"},
    )
    await harness.app.process_update(update)
    expected = "Иван Петров @ivan (Telegram ID: 1234)"
    pending = harness.store().load_session().last_commit.crm_pending
    assert pending[0]["entry"]["report_author"] == expected
    # Повтор нажимает другой пользователь (42), но автор берётся из сохранённой записи.
    await harness.press(harness.card_button("Повторить Битрикс24"))
    assert next(iter(harness.crm.entries.values())).report_author == expected


async def test_delete_tap_removes_the_line_in_place(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м, хомуты под фреон 10 шт")
    before = len(harness.store().load_session().draft.lines)
    await harness.press(harness.card_button("🗑 1"))
    assert len(harness.store().load_session().draft.lines) == before - 1
    assert harness.sent("editMessageText")


async def test_number_input_goes_to_the_card_not_the_engine(harness, monkeypatch):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    await harness.press(harness.card_button("✏️ 1"))
    await harness.press(harness.card_button("Ввести число"))
    assert harness.store().load_session().ui.awaiting_input == "number"

    async def boom(chat_id, text):
        raise AssertionError("the engine must not run for a number")

    monkeypatch.setattr(harness.engine, "handle", boom)
    await harness.text("20")
    assert harness.store().load_session().draft.lines[0].value == 20.0


async def test_button_path_never_calls_the_engine(harness, monkeypatch):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")

    async def boom(chat_id, text):
        raise AssertionError("a button must not call the engine")

    monkeypatch.setattr(harness.engine, "handle", boom)
    await harness.press(harness.card_button("Дата"))
    await harness.press(harness.card_button("Назад"))
    await harness.press(harness.card_button("🗑 1"))


async def test_clarification_tap_runs_the_next_turn(harness, monkeypatch):
    await pick_site(harness)
    turns = []

    async def handle(chat_id, text):
        turns.append(text)
        store = harness.store(chat_id)
        if len(turns) == 1:
            turn_id = store.begin_turn()

            def _ask(s):
                from src.models import Clarification, ClarificationOption

                s.clarification = Clarification(
                    question="Какой день?", options=[ClarificationOption(label="Вчера")], source_text=text
                )
                s.turn_outcome = EngineOutcome(outcome="question", message="Какой день?")

            store.update_session(_ask)
            assert turn_id
            return EngineOutcome(outcome="question", message="Какой день?")
        store.begin_turn()
        return EngineOutcome(outcome="answer", message="Принято: " + text)

    monkeypatch.setattr(harness.engine, "handle", handle)
    await harness.text("что-то неясное")
    assert "Какой день?" in harness.last_text()
    await harness.press(harness.card_button("Вчера"))
    assert turns == ["что-то неясное", "Вчера"]
    assert harness.last_text() == "Принято: Вчера"


async def test_engine_error_offers_retry_with_the_last_text(harness, monkeypatch):
    await pick_site(harness)
    calls = []

    async def handle(chat_id, text):
        calls.append(text)
        harness.store(chat_id).begin_turn()
        if len(calls) == 1:
            return EngineOutcome(outcome="error", message="Не получилось")
        return EngineOutcome(outcome="answer", message="Теперь ок")

    monkeypatch.setattr(harness.engine, "handle", handle)
    await harness.text("медь 6-ку 15 м")
    await harness.press(harness.card_button("Повторить"))
    assert calls == ["медь 6-ку 15 м", "медь 6-ку 15 м"]
    assert harness.last_text() == "Теперь ок"
    users = [h for h in harness.store().load_session().history if h["role"] == "user"]
    assert len(users) == 1


async def test_second_text_is_queued_and_a_callback_changes_nothing(harness, monkeypatch):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    gate = asyncio.Event()
    started = asyncio.Event()
    order = []

    async def handle(chat_id, text):
        order.append(("start", text, chat_id))
        started.set()
        if text == "первое":
            await gate.wait()
        harness.store(chat_id).begin_turn()
        order.append(("end", text, chat_id))
        return EngineOutcome(outcome="answer", message="ответ " + text)

    monkeypatch.setattr(harness.engine, "handle", handle)
    version = harness.store().load_session().draft.version
    delete = harness.card_button("🗑 1")
    first = asyncio.create_task(harness.text("первое"))
    await started.wait()
    second = asyncio.create_task(harness.text("второе"))
    other_chat = asyncio.create_task(harness.text("привет", chat_id=OTHER))
    await asyncio.sleep(0.05)
    await harness.press(delete)
    assert harness.store().load_session().draft.version == version
    assert CALLBACK_BUSY in [r["text"] for r in harness.bot.requests if r["endpoint"] == "answerCallbackQuery"]
    await asyncio.wait_for(other_chat, 2)
    assert "Выберите объект" in harness.last_text(OTHER)
    assert not second.done()
    gate.set()
    await asyncio.gather(first, second)
    assert [e[:2] for e in order if e[2] == CHAT] == [
        ("start", "первое"),
        ("end", "первое"),
        ("start", "второе"),
        ("end", "второе"),
    ]
    assert BUSY_TEXT in [r["text"] for r in harness.sent()]


async def test_partial_failure_reports_and_retries_bitrix_only(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    harness.crm.fail("upsert_entries", times=1)
    await harness.press(harness.card_button("Записать"))
    summary = harness.sent()[-1]
    assert "Таблица обновлена, Битрикс24 — нет" in summary["text"]
    assert harness.crm.entries == {}
    writes_before = harness.gateway.column_of("Садик", DAY)
    await harness.press(harness.card_button("Повторить Битрикс24"))
    assert len(harness.crm.entries) == 1
    assert harness.gateway.column_of("Садик", DAY) == writes_before
    assert harness.last_text().startswith("✅ Записано")


async def test_sheets_failure_writes_nothing_and_retry_works(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    harness.gateway.fail("write_cells", times=1)
    await harness.press(harness.card_button("Записать"))
    assert harness.last_text().startswith("Не записано")
    assert harness.crm.entries == {}
    await harness.press(harness.card_button("Повторить"))
    assert len(harness.crm.entries) == 1
    assert harness.last_text().startswith("✅ Записано")


async def test_today_opens_the_saved_view_after_a_commit(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    await harness.press(harness.card_button("Записать"))
    await harness.command("/today")
    card = harness.sent()[-1]
    assert "Исправление записанного" in card["text"]
    assert harness.store().load_session().draft.kind == "edit_saved"


async def test_day_command_rejects_garbage_and_opens_a_date(harness):
    await pick_site(harness)
    await harness.command("/day")
    assert "Укажите дату" in harness.last_text()
    await harness.command("/day 01.09")
    assert harness.store().load_session().draft.day == date(2026, 9, 1)


async def test_friction_and_mismatch_notices_go_to_support(harness):
    harness.handlers.support_chat_id = 555
    harness.crm.sites = harness.crm.sites[:1]
    await pick_site(harness)
    assert any("не совпадают" in (r["text"] or "") for r in harness.sent(chat_id=555))
    count = len(harness.sent(chat_id=555))
    await harness.command("/site")
    assert len(harness.sent(chat_id=555)) == count  # один раз на набор расхождений

    async def handle(chat_id, text):
        turn = harness.store(chat_id).begin_turn()
        with open(Config.FRICTION_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"chat_id": chat_id, "turn_id": turn, "summary": "не нашёл позицию"}) + "\n")
        return EngineOutcome(outcome="answer", message="ok")

    harness.engine.handle = handle
    await harness.text("что-то")
    assert any("не нашёл позицию" in (r["text"] or "") for r in harness.sent(chat_id=555))


async def test_sites_json_is_written_before_the_turn(harness):
    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    names = {s["name"] for s in json.loads((harness.store().dir / "sites.json").read_text(encoding="utf-8"))}
    assert names == {"Садик", "Школа"}


async def test_vanished_card_message_is_replaced_by_a_new_one(harness, monkeypatch):
    from telegram.error import BadRequest

    await pick_site(harness)
    await harness.text("медь 6-ку 15 м")
    original = harness.bot._post

    async def post(endpoint, data=None, **kwargs):
        if endpoint == "editMessageText":
            raise BadRequest("Message to edit not found")
        return await original(endpoint, data, **kwargs)

    monkeypatch.setattr(harness.bot, "_post", post)
    sends = len(harness.sent())
    await harness.press(harness.card_button("Дата"))
    assert len(harness.sent()) == sends + 1


def test_startup_requires_a_token(monkeypatch):
    from src.bot.app import check_startup

    assert "TELEGRAM_BOT_TOKEN" in check_startup()
    monkeypatch.setattr(Config, "TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setattr(Config, "CODEX_COMMAND", "definitely-not-installed-codex")
    assert check_startup() is None


async def test_heartbeat_file_is_touched(tmp_path, monkeypatch):
    from src.bot import app as app_module

    monkeypatch.setattr(app_module, "HEARTBEAT_EVERY_S", 0.01)
    path = tmp_path / "data" / "heartbeat"
    task = asyncio.create_task(app_module._heartbeat_loop(path))
    await asyncio.sleep(0.05)
    task.cancel()
    assert path.exists()
