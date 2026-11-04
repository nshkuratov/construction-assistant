"""Готовит портал Bitrix24 для бота. Идемпотентно: можно запускать повторно в любое время.

Запуск: ``python -m src.tools.bitrix_setup --site Садик --site Школа`` (или ``--from-sheet``).
Скрипт находит или создаёт смарт-процесс и его пользовательские поля, добавляет недостающие значения
"Объект" (никогда не удаляет и не пересоздаёт их) и пишет ``entityTypeId`` и ключи полей элемента в
``BITRIX_FIELDS_FILE``. Вебхуку нужны права ``crm`` и ``userfieldconfig`` и учётная запись администратора.
"""

import argparse
import json
import sys
from pathlib import Path

from src.adapters.bitrix import BitrixClient, BitrixError
from src.config import Config

TYPE_TITLE = "Ежедневный отчёт мастера"

# (ключ в файле полей, суффикс имени поля, пользовательский тип, подпись)
FIELD_SPECS = (
    ("site", "SITE", "enumeration", "Объект"),
    ("day", "DAY", "date", "Дата"),
    ("number", "NUMBER", "integer", "№ позиции"),
    ("name", "NAME", "string", "Позиция"),
    ("section", "SECTION", "string", "Раздел"),
    ("unit", "UNIT", "string", "Ед. изм."),
    ("volume", "VOLUME", "double", "Объём"),
    ("amount", "AMOUNT", "double", "Сумма"),
    ("chat_id", "CHAT_ID", "string", "Чат-источник"),
    ("position_key", "POSITION_KEY", "string", "Ключ позиции"),
)


def _label(text: str) -> dict:
    return {"ru": text, "en": text}


def _find_type(client: BitrixClient) -> tuple[int, int]:
    types = client.call("crm.type.list").get("result", {}).get("types", [])
    for t in types:
        if t.get("title") == TYPE_TITLE:
            return int(t["id"]), int(t["entityTypeId"])
    body = client.call("crm.type.add", {"fields": {"title": TYPE_TITLE, "isUseInUserfieldEnabled": "Y"}})
    created = body["result"]["type"]
    return int(created["id"]), int(created["entityTypeId"])


def _sync_enum(client: BitrixClient, field: dict, sites: list[str]) -> None:
    # `userfieldconfig.list` не отдаёт "enum"; его возвращает только `get`.
    full = client.call("userfieldconfig.get", {"moduleId": "crm", "id": field["id"]})["result"]["field"]
    existing = full.get("enum", [])
    have = {e["value"] for e in existing}
    new = [{"value": s} for s in dict.fromkeys(sites) if s not in have]
    if new:
        # Обновление заменяет список: отправьте каждый старый элемент целиком, с его id, иначе Bitrix его удалит.
        # Портал игнорирует изменение enum, если идентификатор поля не лежит в том же объекте поля.
        identity = {k: full[k] for k in ("entityId", "fieldName", "userTypeId")}
        client.call(
            "userfieldconfig.update",
            {"moduleId": "crm", "id": field["id"], "field": {**identity, "enum": [*existing, *new]}},
        )


def run_setup(client: BitrixClient, sites: list[str]) -> dict:
    """Готовит портал; возвращает содержимое файла полей."""
    internal_id, type_id = _find_type(client)
    # API полей используют внутренний id; API элементов используют entityTypeId.
    entity = f"CRM_{internal_id}"
    listed = client.call("userfieldconfig.list", {"moduleId": "crm", "filter": {"entityId": entity}})
    user_fields = listed.get("result", {}).get("fields", [])

    def existing(suffix: str) -> dict | None:
        return next((f for f in user_fields if str(f.get("fieldName", "")).upper().endswith(f"_{suffix}")), None)

    for _key, suffix, utype, label in FIELD_SPECS:
        field = existing(suffix)
        if field is None:
            spec = {
                "entityId": entity,
                "fieldName": f"UF_{entity}_{suffix}",
                "userTypeId": utype,
                "editFormLabel": _label(label),
                "listColumnLabel": _label(label),
                "listFilterLabel": _label(label),
            }
            if utype == "enumeration":
                spec["enum"] = [{"value": s} for s in dict.fromkeys(sites)]
            client.call("userfieldconfig.add", {"moduleId": "crm", "field": spec})
        elif utype == "enumeration":
            _sync_enum(client, field, sites)

    item_fields = client.call("crm.item.fields", {"entityTypeId": type_id}).get("result", {}).get("fields", {})
    keys: dict[str, str] = {}
    for key, suffix, _utype, _label_text in FIELD_SPECS:
        for code, info in item_fields.items():
            if str(info.get("upperName", "")).upper().endswith(f"_{suffix}"):
                keys[key] = code
                break
        else:
            raise BitrixError(f"Поле {suffix} не найдено в crm.item.fields после создания.")
    return {"entityTypeId": type_id, "fields": keys}


def _sites_from_sheet() -> list[str]:
    try:
        from src.adapters.factory_sheets import make_sheet_gateway
    except ImportError:
        raise SystemExit("--from-sheet needs the Sheets adapter, which is not in this checkout. Use --site.") from None
    return make_sheet_gateway().list_site_tabs()


def main(argv: list[str] | None = None, client: BitrixClient | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", action="append", default=[], help="site name; repeat for several")
    ap.add_argument("--from-sheet", action="store_true", help="use the tab titles of the sheet")
    args = ap.parse_args(argv)
    sites = list(args.site)
    if args.from_sheet:
        sites += _sites_from_sheet()
    if not sites:
        ap.error("give --site NAME (one or more) or --from-sheet")
    if client is None:
        if not Config.BITRIX_WEBHOOK_URL:
            print("BITRIX_WEBHOOK_URL is not set.", file=sys.stderr)
            return 2
        client = BitrixClient(Config.BITRIX_WEBHOOK_URL)
    try:
        result = run_setup(client, sites)
    except BitrixError as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    path = Path(Config.BITRIX_FIELDS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: entityTypeId={result['entityTypeId']}, fields written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
