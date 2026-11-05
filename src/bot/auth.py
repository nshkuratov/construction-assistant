"""Авторизация по белому списку для интерактивного Telegram-бота (только bot-daemon).

Взято из ``bot.is_allowed`` проекта ``job-assistant`` и отвязано от ``Config``: белый список
передаётся снаружи (или разбирается из окружения через :func:`allowed_chat_ids_from_env`), а не
читается из объекта конфигурации проекта, поэтому у этого транспортного помощника нет зависимостей
от проекта.

Уведомитель с рассылкой (push/broadcast) этим не пользуется: он шлёт в один фиксированный
``TELEGRAM_CHAT_ID`` и никогда не получает команды.
"""

from __future__ import annotations

import os
from collections.abc import Container


def is_allowed(chat_id: int | None, allowed_ids: Container[int]) -> bool:
    """``True`` только если ``chat_id`` есть в ``allowed_ids``. Пустой белый список запрещает всех."""
    return chat_id is not None and chat_id in allowed_ids


def allowed_chat_ids_from_env(var: str = "TELEGRAM_ALLOWED_CHAT_IDS") -> frozenset[int]:
    """Разбирает белый список целых чисел через запятую из окружения в ``frozenset[int]``.

    Пустые элементы и нечисловой мусор пропускаются, поэтому лишняя запятая или пробел в ``.env``
    не роняют запуск бота. Если переменная не задана или пуста, результат пустой ``frozenset``
    (запрещены все).
    """
    raw = os.getenv(var, "") or ""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)
