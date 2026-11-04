"""Помощники для атомарной записи JSON и JSONL.

``atomic_write_json`` пишет во временный файл в том же каталоге, а затем подменяет им целевой
через ``os.replace``, поэтому читатель никогда не видит наполовину записанный файл. Состояние
разных пользователей изолировано: у каждого свой путь, а каждый MCP-инструмент работает в отдельном
короткоживущем подпроцессе. Поэтому у одного файла нет параллельных писателей, и блокировки
внутри процесса здесь не нужны. Это свойство нужно сохранять.
"""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def atomic_write_json(path: str, data, indent: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=os.path.dirname(path) or ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(data, tmp, indent=indent, ensure_ascii=False)
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        os.unlink(tmp.name)
        raise


def safe_load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        logger.warning("Failed to load %s; returning default", path)
        return default


def append_jsonl(path: str, obj: dict) -> None:
    """Дописывает JSON-объект строкой в ``path``; best-effort, вызывающему ошибка не пробрасывается.

    Нужно для журнала аудита и JSONL с обратной связью. Сбой записи не должен ломать вызов
    инструмента, который его породил, поэтому ``OSError`` гасится и пишется в лог, как в
    ``safe_load_json``. ``TypeError`` от ``json.dumps`` на несериализуемом объекте — ошибка
    вызывающего, и её намеренно НЕ гасят.
    """
    # вне try: ошибка сериализации должна всплыть, а не потеряться
    line = json.dumps(obj, ensure_ascii=False)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        logger.warning("Failed to append to %s; dropping the event", path)


def load_jsonl_tail(path: str, limit: int) -> list[dict]:
    """Возвращает до ``limit`` последних разобранных JSON-объектов из файла JSONL.

    Отсутствующий или пустой файл даёт ``[]``, битые строки пропускаются (с записью в лог), как в
    ``safe_load_json``. При ``limit <= 0`` возвращается ``[]`` (обычный срез ``[-limit:]`` был бы
    неверен: ``[-0:]`` в Python возвращает весь список).
    """
    if limit <= 0:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            logger.warning("Skipping malformed JSONL line in %s", path)
    return out
