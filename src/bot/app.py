"""Собирает Telegram ``Application`` из ``Config``, фабрик и движка."""

import asyncio
import logging
import shlex
import shutil
from pathlib import Path

from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from src.adapters.factory_crm import make_crm_sink
from src.adapters.factory_sheets import make_sheet_gateway
from src.bot.auth import allowed_chat_ids_from_env
from src.bot.handlers import BotHandlers
from src.config import Config
from src.engine import make_engine
from src.interfaces.crm import CrmSink
from src.interfaces.engine import Engine
from src.interfaces.sheets import SheetGateway
from src.services.commit import CommitService
from src.services.locks import Locks
from src.services.sites import LiveSiteDirectory
from src.services.snapshots import SnapshotService

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 30
_HEARTBEAT_TASK = "heartbeat_task"


def check_startup() -> str | None:
    """Текст ошибки, если бот не может стартовать; отсутствие бинарника движка только пишет предупреждение в лог."""
    if not Config.TELEGRAM_BOT_TOKEN.strip():
        return "Не задан TELEGRAM_BOT_TOKEN. Получите токен у @BotFather и добавьте его в .env (см. README)."
    if Config.ENGINE_BACKEND.strip().lower() != "codex":
        return None
    binary = (shlex.split(Config.CODEX_COMMAND) or [""])[0]
    if not shutil.which(binary):
        logger.warning(
            "Engine program %r not found. The bot starts, but every report ends in an error. "
            "Install it, or set CODEX_COMMAND (see README).",
            binary,
        )
    return None


def heartbeat_path(data_dir: str | None = None) -> Path:
    return Path(data_dir if data_dir is not None else Config.CA_DATA_DIR) / "heartbeat"


async def _heartbeat_loop(path: Path) -> None:
    """Docker healthcheck читает возраст этого файла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        path.touch()
        await asyncio.sleep(HEARTBEAT_EVERY_S)


def build_handlers(
    engine: Engine | None = None,
    gateway: SheetGateway | None = None,
    crm: CrmSink | None = None,
    data_dir: str | None = None,
    allowed_chat_ids: frozenset[int] | None = None,
) -> BotHandlers:
    gateway = gateway or make_sheet_gateway()
    crm = crm or make_crm_sink()
    directory = LiveSiteDirectory(gateway, crm)
    locks = Locks()
    support = Config.SUPPORT_CHAT_ID.strip()
    return BotHandlers(
        engine=engine or make_engine(),
        gateway=gateway,
        crm=crm,
        directory=directory,
        snapshots=SnapshotService(gateway, directory, data_dir),
        commit=CommitService(gateway, crm, directory, locks, data_dir),
        locks=locks,
        allowed_chat_ids=allowed_chat_ids if allowed_chat_ids is not None else allowed_chat_ids_from_env(),
        support_chat_id=int(support) if support.lstrip("-").isdigit() else None,
        data_dir=data_dir,
    )


def register(application: Application, handlers: BotHandlers) -> None:
    application.add_handler(CommandHandler("start", handlers.cmd_start))
    application.add_handler(CommandHandler("help", handlers.cmd_help))
    application.add_handler(CommandHandler("today", handlers.cmd_today))
    application.add_handler(CommandHandler("day", handlers.cmd_day))
    application.add_handler(CommandHandler("site", handlers.cmd_site))
    application.add_handler(CommandHandler("sheet", handlers.cmd_sheet))
    application.add_handler(CallbackQueryHandler(handlers.on_callback))
    media = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.VIDEO
    application.add_handler(MessageHandler(media, handlers.on_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    application.add_error_handler(_log_error)


async def _log_error(update: object, context) -> None:
    logger.error("unhandled error while processing an update", exc_info=context.error)


def build_application(token: str | None = None, handlers: BotHandlers | None = None) -> Application:
    handlers = handlers or build_handlers()
    path = heartbeat_path(handlers.data_dir)

    async def post_init(application: Application) -> None:
        application.bot_data[_HEARTBEAT_TASK] = asyncio.create_task(_heartbeat_loop(path))

    async def post_shutdown(application: Application) -> None:
        task = application.bot_data.get(_HEARTBEAT_TASK)
        if task is not None:
            task.cancel()

    application = (
        ApplicationBuilder()
        .token(token or Config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    register(application, handlers)
    return application
