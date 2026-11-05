"""Приёмник Bitrix24 (смарт-процесс) через входящий вебхук (REST).

Элементы определяются тройкой (значение перечисления объекта, день, ``position_key``). Записывается
итоговое значение ячейки за день, поэтому повтор того же вызова даёт то же состояние.
"""

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from urllib.parse import quote

import httpx

from src.interfaces.errors import CrmError
from src.models import CrmEntry, CrmSite

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BATCH_LIMIT = 50
BATCH_MIN_OPS = 4  # больше 3 операций отправляются через ``batch``
PAGE_SIZE = 50
REQUIRED_FIELDS = ("site", "day", "position_key", "number", "name", "section", "unit", "volume", "amount")
_TOKEN_RE = re.compile(r"(/rest/\d+/)[^/\s\"']+")


class BitrixError(CrmError):
    """Вызов Bitrix24 не удался. Сообщение короткое, на русском и не содержит секретов."""


def mask_webhook(text: str) -> str:
    """Скрывает токен в URL вебхука (``/rest/<user>/<token>/``) в любом тексте."""
    return _TOKEN_RE.sub(r"\1***", text)


def _php_pairs(value, prefix: str) -> list[tuple[str, str]]:
    """Разворачивает вложенные параметры в стиль PHP-запроса (``fields[a][b]=1``), как нужно командам ``batch``."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _php_pairs(v, f"{prefix}[{k}]")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _php_pairs(v, f"{prefix}[{i}]")]
    if value is None:
        return [(prefix, "")]
    return [(prefix, str(value))]


def _batch_command(method: str, params: dict) -> str:
    pairs = [p for k, v in params.items() for p in _php_pairs(v, k)]
    return f"{method}?" + "&".join(f"{quote(k, safe='[]')}={quote(v, safe='')}" for k, v in pairs)


def _position_number(position_key: str) -> int | None:
    head = position_key.split("|", 1)[0]
    return int(head) if head.isdigit() else None


class BitrixClient:
    """REST-транспорт через вебхук с повторами и маскировкой секрета."""

    def __init__(
        self,
        webhook_url: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base = webhook_url.rstrip("/") + "/"
        self._sleep = sleep
        self._client = httpx.Client(transport=transport, timeout=30.0)
        # httpx пишет URL каждого запроса на уровне INFO, а в URL лежит токен вебхука.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    # ---- transport ------------------------------------------------------------------------------
    def call(self, method: str, params: dict | None = None) -> dict:
        """Вызывает REST-метод через POST, возвращает тело ответа. Повторяет при лимите запросов и 429/5xx."""
        last = "нет ответа"
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                resp = self._client.post(self._base + method, json=params or {})
            except httpx.HTTPError as exc:
                last = f"сбой сети ({type(exc).__name__})"
                logger.warning("bitrix %s: %s, attempt %d", method, last, attempt + 1)
                continue
            retryable = resp.status_code == 429 or resp.status_code >= 500
            try:
                body = resp.json()
            except ValueError:
                if retryable:
                    last = f"HTTP {resp.status_code}"
                    continue
                raise BitrixError(f"Битрикс24 вернул не JSON (HTTP {resp.status_code}).") from None
            if not isinstance(body, dict):
                raise BitrixError("Битрикс24 вернул неожиданный ответ.")
            code = body.get("error")
            if code == "QUERY_LIMIT_EXCEEDED" or (code and retryable) or (retryable and not code):
                last = str(code or f"HTTP {resp.status_code}")
                logger.warning("bitrix %s: %s, attempt %d", method, last, attempt + 1)
                continue
            if code:
                desc = mask_webhook(str(body.get("error_description", "")))[:200]
                raise BitrixError(f"Битрикс24 отклонил запрос {method}: {code}. {desc}".strip())
            return body
        raise BitrixError(f"Битрикс24 не отвечает ({method}): {last}.")


class BitrixSink(BitrixClient):
    def __init__(
        self,
        webhook_url: str,
        entity_type_id: int,
        fields: dict[str, str],
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        missing = [k for k in REQUIRED_FIELDS if k not in fields]
        if missing:
            raise BitrixError(f"В файле полей Битрикс24 нет: {', '.join(missing)}. Запустите bitrix_setup.")
        super().__init__(webhook_url, transport, sleep)
        self._type = entity_type_id
        self._f = fields
        if "report_author" not in fields:
            logger.warning(
                "Нет поля «Автор отчёта» в настройке Битрикс24. "
                "Запустите python -m src.tools.bitrix_setup --from-sheet и перезапустите бота."
            )

    def _batch(self, ops: list[tuple[str, dict]]) -> None:
        for i in range(0, len(ops), BATCH_LIMIT):
            chunk = ops[i : i + BATCH_LIMIT]
            cmd = {f"c{n}": _batch_command(m, p) for n, (m, p) in enumerate(chunk)}
            result = self.call("batch", {"halt": 0, "cmd": cmd}).get("result", {})
            errors = result.get("result_error") or {}
            if errors:
                first = next(iter(errors.values()))
                raise BitrixError(f"Битрикс24: {len(errors)} из {len(chunk)} операций не выполнены ({first}).")

    def _run(self, ops: list[tuple[str, dict]]) -> None:
        if len(ops) >= BATCH_MIN_OPS:
            self._batch(ops)
        else:
            for method, params in ops:
                self.call(method, params)

    # ---- reads ----------------------------------------------------------------------------------
    def list_sites(self) -> list[CrmSite]:
        fields = self.call("crm.item.fields", {"entityTypeId": self._type}).get("result", {}).get("fields", {})
        info = fields.get(self._f["site"])
        if info is None:
            raise BitrixError("В Битрикс24 нет поля «Объект». Запустите bitrix_setup.")
        return [CrmSite(value_id=str(it["ID"]), name=str(it["VALUE"])) for it in info.get("items", [])]

    def _find(self, site_value: str, day: date, keys: list[str]) -> dict[str, list[int]]:
        """Id элементов по ключу позиции для одного объекта и дня; ключи сверяются точно, дважды."""
        found: dict[str, list[int]] = defaultdict(list)
        wanted = set(keys)
        start = 0
        while True:
            body = self.call(
                "crm.item.list",
                {
                    "entityTypeId": self._type,
                    "select": ["id", self._f["position_key"]],
                    "filter": {
                        f"={self._f['site']}": site_value,
                        f"={self._f['day']}": day.isoformat(),
                        f"={self._f['position_key']}": keys,
                    },
                    "start": start,
                },
            )
            for item in body.get("result", {}).get("items", []):
                key = item.get(self._f["position_key"])
                if key in wanted:
                    found[key].append(int(item["id"]))
            if "next" not in body:
                break
            start = int(body["next"])
        return found

    # ---- writes ---------------------------------------------------------------------------------
    def _fields_of(self, e: CrmEntry) -> dict:
        f = self._f
        amount = round(e.value * e.unit_price, 2) if e.unit_price is not None else None
        fields = {
            "title": f"{e.day.isoformat()} {e.position_name}"[:250],
            f["site"]: e.crm_value_id,
            f["day"]: e.day.isoformat(),
            f["position_key"]: e.position_key,
            f["number"]: _position_number(e.position_key),
            f["name"]: e.position_name,
            f["section"]: e.section,
            f["unit"]: e.unit,
            f["volume"]: e.value,
            f["amount"]: amount,
        }
        # Старые отложенные записи без автора не должны стирать уже известное имя.
        if e.report_author and "report_author" in f:
            fields[f["report_author"]] = e.report_author
        return fields

    def _groups(self, entries: list[CrmEntry]) -> dict[tuple[str, date], dict[str, CrmEntry]]:
        groups: dict[tuple[str, date], dict[str, CrmEntry]] = defaultdict(dict)
        for e in entries:
            groups[(e.crm_value_id, e.day)][e.position_key] = e
        return groups

    def upsert_entries(self, entries: list[CrmEntry]) -> None:
        ops: list[tuple[str, dict]] = []
        for (site_value, day), by_key in self._groups(entries).items():
            found = self._find(site_value, day, list(by_key))
            for key, entry in by_key.items():
                ids = found.get(key, [])
                fields = self._fields_of(entry)
                if ids:
                    ops.append(("crm.item.update", {"entityTypeId": self._type, "id": ids[0], "fields": fields}))
                    ops.extend(("crm.item.delete", {"entityTypeId": self._type, "id": extra}) for extra in ids[1:])
                else:
                    ops.append(("crm.item.add", {"entityTypeId": self._type, "fields": fields}))
        self._run(ops)

    def delete_entries(self, entries: list[CrmEntry]) -> None:
        ops: list[tuple[str, dict]] = []
        for (site_value, day), by_key in self._groups(entries).items():
            for ids in self._find(site_value, day, list(by_key)).values():
                ops.extend(("crm.item.delete", {"entityTypeId": self._type, "id": i}) for i in ids)
        self._run(ops)
