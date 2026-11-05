"""``python -m src.bot``: запускает бота с long polling."""

import logging
import re
import sys

from src.bot.app import build_application, check_startup
from src.config import Config

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def configure_logging(level: str) -> None:
    """Настраивает логирование так, чтобы ни одна строка лога не содержала токен бота.

    httpx пишет URL каждого запроса на уровне INFO, а URL Telegram Bot API содержит токен. Логгеры
    HTTP-клиента переведены на WARNING, а каждая запись дополнительно маскируется как вторая защита.
    """
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    base_factory = logging.getLogRecordFactory()
    if getattr(base_factory, "masks_token", False):
        return

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        message = record.getMessage()
        if _TOKEN_RE.search(message):
            record.msg = _TOKEN_RE.sub("bot***", message)
            record.args = ()
        return record

    factory.masks_token = True
    logging.setLogRecordFactory(factory)


def main() -> int:
    configure_logging(Config.LOG_LEVEL)
    problem = check_startup()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    build_application().run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
