"""Обработчики Telegram: тонкий слой над конечным автоматом карточки, сервисами и движком.

Конкурентность: один ``asyncio.Lock`` на чат защищает каждое изменение сессии (тексты, кнопки,
команды). Ход движка держит его весь ход; текст, пришедший за это время, ждёт в FIFO-очереди замка,
поэтому один чат выполняет один ход за раз, а другие чаты не блокируются. Движок пишет сессию через
MCP-инструменты, поэтому бот после хода перечитывает её с диска и никогда не записывает обратно
копию, прочитанную до хода.
"""

import asyncio
import logging
from datetime import date

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.bot.auth import is_allowed
from src.bot.card import (
    Button,
    CallbackError,
    CancelIntent,
    CardEnv,
    CommitIntent,
    EngineIntent,
    RetryIntent,
    apply,
    apply_text,
    decode,
    encode_card,
    encode_retry,
    format_volume,
    render,
)
from src.bot.card.actions import Action
from src.bot.telegram_view import CardMessenger
from src.config import Config
from src.domain.date_parse import resolve_date
from src.domain.dates import today as today_in_tz
from src.interfaces.crm import CrmSink
from src.interfaces.engine import Engine
from src.interfaces.errors import CrmError, SheetsError
from src.interfaces.sheets import SheetGateway
from src.interfaces.sites import SiteDirectory
from src.matching.candidates import find_candidates
from src.models import CatalogSnapshot, ChatSession, CommitResult, Draft, EngineOutcome, Site, SiteListing, UiState
from src.services.commit import CommitRefused, CommitService
from src.services.locks import Locks
from src.services.snapshots import SnapshotService
from src.session.store import SessionStore, new_draft_id
from src.utils.persistence import atomic_write_json, load_jsonl_tail
from src.utils.timeutil import now_utc

logger = logging.getLogger(__name__)

REFUSAL = "Доступ закрыт. Попросите администратора добавить ваш чат (чат ID: {chat_id})."
BUSY_TEXT = "⏳ Ещё разбираю предыдущее сообщение — обработаю следом"
WORKING_TEXT = "⏳ Разбираю отчёт…"
CALLBACK_BUSY = "⏳ Подождите…"
VOICE_HINT = (
    "Голосовые сообщения я не слушаю. Нажмите на значок микрофона на клавиатуре Telegram, "
    "продиктуйте отчёт и отправьте получившийся текст."
)
HELP_TEXT = (
    "Я помогаю вносить ежедневный отчёт в таблицу и в Битрикс24.\n\n"
    "Напишите, что сделано и сколько, например:\n"
    "«Сегодня проложили медь 6-ку 15 м, кабель 3х2,5 — 56 м, поставили 10 хомутов»\n"
    "Можно продиктовать текст микрофоном на клавиатуре Telegram.\n\n"
    "Я покажу список. В нём можно изменить (✏️), удалить (🗑) или добавить (➕) строку, "
    "выбрать дату (📅) и объект (🏗). Пока вы не нажмёте «✅ Записать», ничего не записывается.\n\n"
    "Команды:\n"
    "/today — записанное за сегодня\n"
    "/day 02.11 — записанное за другой день\n"
    "/site — выбрать объект\n"
    "/sheet — ссылка на таблицу\n"
    "/help — эта справка\n\n"
    "Можно спросить: «сколько осталось по кабелю 3х2,5?»"
)
MAX_PLAIN_CHARS = 4000
MAX_SUMMARY_LINES = 20
_FRICTION_TAIL = 50


def _empty_snapshot(site_id: str) -> CatalogSnapshot:
    return CatalogSnapshot(site_id=site_id, taken_at=now_utc())


def _search(snapshot: CatalogSnapshot, query: str) -> list[int]:
    return [c.position.id for c in find_candidates(query, snapshot, limit=20)]


class BotHandlers:
    def __init__(
        self,
        *,
        engine: Engine,
        gateway: SheetGateway,
        crm: CrmSink,
        directory: SiteDirectory,
        snapshots: SnapshotService,
        commit: CommitService,
        locks: Locks,
        allowed_chat_ids: frozenset[int],
        support_chat_id: int | None = None,
        data_dir: str | None = None,
    ):
        self.engine = engine
        self.gateway = gateway
        self.crm = crm
        self.directory = directory
        self.snapshots = snapshots
        self.commit = commit
        self.locks = locks
        self.allowed = allowed_chat_ids
        self.support_chat_id = support_chat_id
        self.data_dir = data_dir
        self._engine_busy: set[int] = set()
        self._notified_mismatches: tuple[str, ...] | None = None

    # ------------------------------------------------------------------ helpers

    def _store(self, chat_id: int) -> SessionStore:
        return SessionStore(chat_id, self.data_dir)

    async def _guard(self, update: Update) -> int | None:
        """id разрешённого чата либо ``None`` после короткого отказа."""
        chat = update.effective_chat
        if chat is None:
            return None
        if not is_allowed(chat.id, self.allowed):
            refusal = REFUSAL.format(chat_id=chat.id)
            if update.callback_query is not None:
                await update.callback_query.answer(refusal, show_alert=True)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(refusal)
            return None
        return chat.id

    async def _listing(self, bot) -> SiteListing:
        listing = await asyncio.to_thread(self.directory.list_sites)
        key = tuple(sorted(listing.mismatches))
        if key and key != self._notified_mismatches:
            self._notified_mismatches = key
            logger.warning("site directory mismatches: %s", "; ".join(key))
            await self._support(bot, "⚠️ Объекты в Google Sheets и Битрикс24 не совпадают:\n" + "\n".join(key))
        elif not key:
            self._notified_mismatches = None
        return listing

    async def _support(self, bot, text: str) -> None:
        if self.support_chat_id is None:
            return
        try:
            await bot.send_message(self.support_chat_id, text)
        except TelegramError as exc:
            logger.warning("support notice not delivered: %s", type(exc).__name__)

    def _env(self, store: SessionStore, listing: SiteListing) -> CardEnv:
        today = today_in_tz()

        def parse_date(text: str) -> date | None:
            found = resolve_date(text, today)
            return found.day if found else None

        return CardEnv(
            sites=listing.sites,
            today=today,
            search=_search,
            parse_date=parse_date,
            load_snapshot=store.read_snapshot,
        )

    @staticmethod
    def _snapshot(store: SessionStore, session: ChatSession) -> CatalogSnapshot:
        site_id = session.site_id or ""
        return (store.read_snapshot(site_id) if site_id else None) or _empty_snapshot(site_id)

    @staticmethod
    def _write_sites(store: SessionStore, listing: SiteListing) -> None:
        atomic_write_json(str(store.dir / "sites.json"), [s.model_dump(mode="json") for s in listing.sites])

    async def _refresh(self, chat_id: int, listing: SiteListing, session: ChatSession, force: bool = False) -> None:
        ids = [s.site_id for s in listing.sites]
        if session.site_id in ids:
            ids.remove(session.site_id)
            ids.insert(0, session.site_id)
        await self.snapshots.refresh(chat_id, ids, force=force)

    def _has_site(self, session: ChatSession, listing: SiteListing) -> bool:
        return session.site_id is not None and any(s.site_id == session.site_id for s in listing.sites)

    async def _send_card(self, bot, chat_id: int, store: SessionStore, listing: SiteListing) -> None:
        """Отправляет НОВОЕ сообщение карточки (старая клавиатура убирается) и запоминает его id."""
        session = store.load_session()
        view = render(session, self._snapshot(store, session), self._env(store, listing))
        new_id = await CardMessenger(bot).send_new(chat_id, view, session.card_message_id)
        store.update_session(lambda s: setattr(s, "card_message_id", new_id))

    async def _show_picker(self, bot, chat_id: int, store: SessionStore, listing: SiteListing) -> None:
        def _pick(s: ChatSession) -> None:
            s.site_id = None if not self._has_site(s, listing) else s.site_id
            s.ui = UiState(view="site_pick")

        store.update_session(_pick)
        await self._send_card(bot, chat_id, store, listing)

    # ------------------------------------------------------------------ commands

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update) is None:
            return
        await update.effective_message.reply_text(HELP_TEXT)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(HELP_TEXT)
        async with self.locks.chat_lock(chat_id):
            store = self._store(chat_id)
            listing = await self._listing(context.bot)
            if not self._has_site(store.load_session(), listing):
                await self._show_picker(context.bot, chat_id, store, listing)

    async def cmd_site(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        async with self.locks.chat_lock(chat_id):
            store = self._store(chat_id)
            listing = await self._listing(context.bot)
            await self._show_picker(context.bot, chat_id, store, listing)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._open_saved(update, context, None)

    async def cmd_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args or [])
        found = resolve_date(text, today_in_tz()) if text else None
        if found is None:
            if await self._guard(update) is not None:
                await update.effective_message.reply_text("Укажите дату, например: /day 02.11 или /day вчера")
            return
        await self._open_saved(update, context, found.day)

    async def _open_saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE, day: date | None) -> None:
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        async with self.locks.chat_lock(chat_id):
            bot, store = context.bot, self._store(chat_id)
            listing = await self._listing(bot)
            session = store.load_session()
            if not self._has_site(session, listing):
                await self._show_picker(bot, chat_id, store, listing)
                return
            if session.draft is not None and session.draft.kind == "new" and session.draft.lines:
                await update.effective_message.reply_text("Сначала запишите или отмените отчёт.")
                return
            await self._refresh(chat_id, listing, session, force=True)
            env = self._env(store, listing)
            target = day or env.today
            session.draft = Draft(
                draft_id=new_draft_id(),
                site_id=session.site_id,
                day=target,
                day_source="user" if day else "default",
            )
            result = apply(session, Action("saved"), self._snapshot(store, session), env)
            store.save_session(result.session)
            await self._send_card(bot, chat_id, store, listing)

    async def cmd_sheet(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        store = self._store(chat_id)
        listing = await self._listing(context.bot)
        session = store.load_session()
        site = next((s for s in listing.sites if s.site_id == session.site_id), None)
        if site is None:
            async with self.locks.chat_lock(chat_id):
                await self._show_picker(context.bot, chat_id, store, listing)
            return
        await CardMessenger(context.bot).plain(
            chat_id,
            f"Таблица объекта «{site.name}»:",
            [[Button("📊 Открыть таблицу", url=self.gateway.sheet_url(site.sheet_tab))]],
        )

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update) is None:
            return
        await update.effective_message.reply_text(VOICE_HINT)

    # ------------------------------------------------------------------ text

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Проверяется до первого await, поэтому сообщение, пришедшее посреди хода, всегда видно.
        chat = update.effective_chat
        queued = chat is not None and chat.id in self._engine_busy
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        if queued:
            await update.effective_message.reply_text(BUSY_TEXT)
        async with self.locks.chat_lock(chat_id):
            bot, store = context.bot, self._store(chat_id)
            listing = await self._listing(bot)
            session = store.load_session()
            if not self._has_site(session, listing):
                await self._show_picker(bot, chat_id, store, listing)
                return
            if session.ui.awaiting_input is None:
                await self._engine_turn(bot, chat_id, text, listing)
                return
            await self._refresh(chat_id, listing, session)
            env = self._env(store, listing)
            result = apply_text(session, text, self._snapshot(store, session), env)
            store.save_session(result.session)
            if result.unparsed_text is not None:
                await self._engine_turn(bot, chat_id, result.unparsed_text, listing)
                return
            if result.notice:
                await CardMessenger(bot).plain(chat_id, result.notice)
            await self._edit_card(bot, chat_id, store, listing)

    async def _edit_card(
        self, bot, chat_id: int, store: SessionStore, listing: SiteListing, message_id: int | None = None
    ) -> None:
        session = store.load_session()
        view = render(session, self._snapshot(store, session), self._env(store, listing))
        new_id = await CardMessenger(bot).edit(chat_id, message_id or session.card_message_id, view)
        if new_id != session.card_message_id:
            store.update_session(lambda s: setattr(s, "card_message_id", new_id))

    # ------------------------------------------------------------------ engine turn

    async def _engine_turn(self, bot, chat_id: int, text: str, listing: SiteListing, record: bool = True) -> None:
        """Выполняет один ход движка. Вызывающий держит замок чата."""
        self._engine_busy.add(chat_id)
        messenger = CardMessenger(bot)
        try:
            status_id = await messenger.plain(chat_id, WORKING_TEXT)
            store = self._store(chat_id)
            try:
                await self._refresh(chat_id, listing, store.load_session())
                self._write_sites(store, listing)
                if record:
                    store.update_session(lambda s: s.history.append({"role": "user", "text": text}))
                outcome = await self.engine.handle(chat_id, text)
            except (SheetsError, CrmError) as exc:
                logger.warning("turn prepare failed: %s", exc)
                outcome = EngineOutcome(outcome="error", message="Не удалось прочитать таблицу. Попробуйте ещё раз.")
            session = store.update_session(
                lambda s: s.history.append(
                    {"role": "assistant", "outcome": outcome.outcome, "text": outcome.message[:300]}
                )
            )
            try:
                await bot.delete_message(chat_id, status_id)
            except TelegramError as exc:
                logger.info("status message not deleted: %s", type(exc).__name__)
            await self._deliver(bot, chat_id, store, listing, session, outcome)
            await self._report_friction(bot, chat_id, session)
        finally:
            self._engine_busy.discard(chat_id)

    async def _deliver(
        self, bot, chat_id: int, store: SessionStore, listing: SiteListing, session: ChatSession, outcome: EngineOutcome
    ) -> None:
        messenger = CardMessenger(bot)
        if outcome.outcome in ("draft", "question"):
            if outcome.outcome == "question" and session.clarification is None and outcome.message:
                await messenger.plain(chat_id, outcome.message[:MAX_PLAIN_CHARS])
            await self._send_card(bot, chat_id, store, listing)
        elif outcome.outcome == "error":
            await messenger.plain(chat_id, outcome.message, [[Button("🔁 Повторить", encode_retry("engine"))]])
        else:
            await messenger.plain(chat_id, outcome.message[:MAX_PLAIN_CHARS] or "Готово.")

    async def _report_friction(self, bot, chat_id: int, session: ChatSession) -> None:
        if self.support_chat_id is None or session.turn_id is None:
            return
        for entry in load_jsonl_tail(Config.FRICTION_LOG, _FRICTION_TAIL):
            if entry.get("chat_id") == chat_id and entry.get("turn_id") == session.turn_id:
                await self._support(bot, f"⚠️ Трудность в чате {chat_id}: {entry.get('summary', '')}")

    # ------------------------------------------------------------------ buttons

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat = update.effective_chat
        busy = chat is not None and chat.id in self._engine_busy and is_allowed(chat.id, self.allowed)
        if busy:
            await query.answer(CALLBACK_BUSY)
            return
        chat_id = await self._guard(update)
        if chat_id is None:
            return
        await query.answer()
        try:
            action = decode(query.data or "")
        except CallbackError:
            logger.info("ignored callback data %r", query.data)
            return
        message_id = query.message.message_id if query.message else None
        user = query.from_user
        name = " ".join(user.full_name.split())
        username = f" @{user.username}" if user.username else ""
        report_author = f"{name}{username} (Telegram ID: {user.id})"
        async with self.locks.chat_lock(chat_id):
            await self._on_action(context.bot, chat_id, action, message_id, report_author=report_author)

    async def _on_action(
        self, bot, chat_id: int, action: Action, message_id: int | None, *, report_author: str = ""
    ) -> None:
        store = self._store(chat_id)
        listing = await self._listing(bot)
        session = store.load_session()
        try:
            await self._refresh(chat_id, listing, session, force=action.name == "saved")
        except (SheetsError, CrmError) as exc:
            logger.warning("snapshot refresh failed: %s", exc)
            await CardMessenger(bot).plain(chat_id, "Не удалось прочитать таблицу. Попробуйте ещё раз.")
            return
        env = self._env(store, listing)
        result = apply(session, action, self._snapshot(store, session), env)
        if not result.stale:
            store.save_session(result.session)
        if result.notice:
            await CardMessenger(bot).plain(chat_id, result.notice)
        intent = result.intent
        if isinstance(intent, CommitIntent):
            await self._do_commit(bot, chat_id, store, listing, intent, report_author=report_author)
        elif isinstance(intent, CancelIntent):
            store.update_session(self._clear_draft)
            await self._edit_card(bot, chat_id, store, listing, message_id)
        elif isinstance(intent, EngineIntent):
            await self._engine_turn(bot, chat_id, intent.text, listing)
        elif isinstance(intent, RetryIntent):
            await self._do_retry(bot, chat_id, store, listing, intent.kind)
        elif result.stale or message_id == session.card_message_id or message_id is None:
            await self._edit_card(bot, chat_id, store, listing, message_id)
        else:
            await self._send_card(bot, chat_id, store, listing)

    @staticmethod
    def _clear_draft(s: ChatSession) -> None:
        s.draft = None
        s.clarification = None
        s.ui = UiState()

    async def _do_retry(self, bot, chat_id: int, store: SessionStore, listing: SiteListing, kind: str) -> None:
        if kind == "crm":
            try:
                result = await self.commit.retry_crm(chat_id)
            except CommitRefused:
                await CardMessenger(bot).plain(chat_id, "Нечего повторять.")
                return
            await self._report_commit(bot, chat_id, store, listing, result, None)
            return
        last = next((h["text"] for h in reversed(store.load_session().history) if h.get("role") == "user"), None)
        if last is None:
            await CardMessenger(bot).plain(chat_id, "Не нашёл сообщение для повтора. Отправьте отчёт заново.")
            return
        await self._engine_turn(bot, chat_id, last, listing, record=False)

    # ------------------------------------------------------------------ commit

    async def _do_commit(
        self, bot, chat_id: int, store: SessionStore, listing: SiteListing, intent: CommitIntent,
        *, report_author: str = "",
    ) -> None:
        messenger = CardMessenger(bot)
        try:
            result = await self.commit.commit(chat_id, intent.draft_id, intent.version, report_author=report_author)
        except CommitRefused as exc:
            logger.info("commit refused: %s", exc)
            await self._edit_card(bot, chat_id, store, listing)
            return
        session = store.load_session()
        if not result.sheets_ok:
            retry = encode_card(intent.draft_id, intent.version, "ok")
            reason = "; ".join(result.errors) or "ошибка записи"
            await messenger.plain(
                chat_id,
                f"Не записано: {reason}\nНичего не изменилось, отчёт сохранён.",
                [[Button("🔁 Повторить", retry)]],
            )
            return
        old_card = session.card_message_id
        store.update_session(lambda s: (self._clear_draft(s), setattr(s, "card_message_id", None)))
        if old_card is not None:
            await messenger.strip_keyboard(chat_id, old_card)
        await self._refresh(chat_id, listing, session, force=True)
        await self._report_commit(bot, chat_id, store, listing, result, session)

    async def _report_commit(
        self,
        bot,
        chat_id: int,
        store: SessionStore,
        listing: SiteListing,
        result: CommitResult,
        session: ChatSession | None,
    ) -> None:
        session = session or store.load_session()
        site: Site | None = next((s for s in listing.sites if s.site_id == session.site_id), None)
        rows: list[list[Button]] = []
        if site is not None:
            rows.append([Button("📊 Открыть таблицу", url=self.gateway.sheet_url(site.sheet_tab))])
        if not result.crm_ok:
            rows.append([Button("🔁 Повторить Битрикс24", encode_retry("crm"))])
        rows.append([Button("📋 Записанное", encode_retry("saved"))])
        if result.crm_ok:
            head = "✅ Записано в таблицу и в Битрикс24"
        else:
            head = "⚠️ Таблица обновлена, Битрикс24 — нет"
        lines = [head]
        snap = self._snapshot(store, session)
        units = {p.id: p.unit for p in snap.positions}
        for item in result.written[:MAX_SUMMARY_LINES]:
            value = item["value"]
            shown = "удалено" if value is None else format_volume(value, units.get(item["position_id"], ""))
            lines.append(f"• {item['name']} — {shown}")
        if len(result.written) > MAX_SUMMARY_LINES:
            lines.append(f"… и ещё {len(result.written) - MAX_SUMMARY_LINES}")
        if not result.crm_ok:
            lines.append(f"Не дошло до Битрикс24: {len(result.crm_pending)}. Нажмите «Повторить Битрикс24».")
        await CardMessenger(bot).plain(chat_id, "\n".join(lines), rows)
