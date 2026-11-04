"""BitrixSink и инструмент настройки против портала с состоянием в том же процессе (httpx MockTransport).

Портал отражает наше понимание контракта REST (вид фильтра, ключи полей); оно подтверждается
только проверкой на живом портале.
"""

import json
import logging
from datetime import date
from urllib.parse import parse_qsl

import httpx
import pytest

from src.adapters.bitrix import BitrixClient, BitrixError, BitrixSink, mask_webhook
from src.adapters.factory_crm import make_crm_sink
from src.config import Config
from src.interfaces.fakes import FakeCrmSink
from src.models import CrmEntry
from src.tools import bitrix_setup

TOKEN = "s3cr3ttoken"
URL = f"https://portal.example/rest/1/{TOKEN}/"
FIELDS = {
    "site": "ufCrm5Site",
    "day": "ufCrm5Day",
    "position_key": "ufCrm5PositionKey",
    "number": "ufCrm5Number",
    "name": "ufCrm5Name",
    "section": "ufCrm5Section",
    "unit": "ufCrm5Unit",
    "volume": "ufCrm5Volume",
    "amount": "ufCrm5Amount",
}
DAY = date(2026, 11, 3)


def _unflatten(pairs):
    out: dict = {}
    for k, v in pairs:
        parts = k.replace("]", "").split("[")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


class Portal:
    """Минимальный Bitrix24: элементы смарт-процесса, а также API типов и пользовательских полей."""

    def __init__(self):
        self.items: dict[int, dict] = {}
        self.next_id = 1
        self.log: list[str] = []
        self.failures: list[httpx.Response | Exception] = []
        self.fail_after_apply = False
        self.only: str | None = None
        self.enum = [{"ID": "11", "VALUE": "Садик"}, {"ID": "12", "VALUE": "Школа"}]
        self.types: list[dict] = []
        self.user_fields: list[dict] = []
        self.next_uf = 1
        self.page = 50

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.log.append(method)
        armed = bool(self.failures) and self.only in (None, method)
        if armed and not self.fail_after_apply:
            f = self.failures.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        resp = httpx.Response(200, json=self.dispatch(method, params))
        if armed and self.fail_after_apply:
            f = self.failures.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        return resp

    def dispatch(self, method, p):
        if method == "batch":
            result, errors = {}, {}
            for name, cmd in p["cmd"].items():
                m, _, qs = cmd.partition("?")
                body = self.dispatch(m, _unflatten(parse_qsl(qs, keep_blank_values=True)))
                if "error" in body:
                    errors[name] = body["error"]
                else:
                    result[name] = body["result"]
            return {"result": {"result": result, "result_error": errors}}
        if method == "crm.item.fields":
            return {
                "result": {
                    "fields": {**self._upper(), FIELDS["site"]: {"upperName": "UF_CRM_5_SITE", "items": self.enum}}
                }
            }
        if method == "crm.item.list":
            flt = p["filter"]
            rows = [
                {"id": i, **it}
                for i, it in self.items.items()
                if str(it[FIELDS["site"]]) == flt[f"={FIELDS['site']}"]
                and it[FIELDS["day"]] == flt[f"={FIELDS['day']}"]
                and it[FIELDS["position_key"]] in flt[f"={FIELDS['position_key']}"]
            ]
            start = p.get("start", 0)
            page = rows[start : start + self.page]
            body = {"result": {"items": page}}
            if start + self.page < len(rows):
                body["next"] = start + self.page
            return body
        if method == "crm.item.add":
            fields = {k: str(v) if isinstance(v, int) and k == FIELDS["site"] else v for k, v in p["fields"].items()}
            self.items[self.next_id] = fields
            self.next_id += 1
            return {"result": {"item": {"id": self.next_id - 1}}}
        if method == "crm.item.update":
            iid = int(p["id"])
            if iid not in self.items:
                return {"error": "NOT_FOUND"}
            self.items[iid].update(p["fields"])
            return {"result": {"item": {"id": iid}}}
        if method == "crm.item.delete":
            if int(p["id"]) not in self.items:
                return {"error": "NOT_FOUND", "error_description": "gone"}
            del self.items[int(p["id"])]
            return {"result": []}
        if method == "crm.type.list":
            return {"result": {"types": self.types}}
        if method == "crm.type.add":
            t = {"id": 5, "title": p["fields"]["title"], "entityTypeId": 1040}
            self.types.append(t)
            return {"result": {"type": t}}
        if method == "userfieldconfig.list":
            if p["filter"]["entityId"] != "CRM_5":
                return {"error": "ACCESS_DENIED", "error_description": "Unknown field entity"}
            # Настоящий вызов list не отдаёт "enum"; его возвращает только userfieldconfig.get.
            return {"result": {"fields": [{k: v for k, v in f.items() if k != "enum"} for f in self.user_fields]}}
        if method == "userfieldconfig.get":
            f = next((x for x in self.user_fields if x["id"] == p["id"]), None)
            return {"result": {"field": f}} if f else {"error": "NOT_FOUND"}
        if method == "userfieldconfig.add":
            if p["field"]["entityId"] != "CRM_5" or not p["field"]["fieldName"].startswith("UF_CRM_5_"):
                return {"error": "INVALID_FIELD", "error_description": "Full field name and entity required"}
            f = dict(p["field"], id=self.next_uf)
            self.next_uf += 1
            if "enum" in f:
                f["enum"] = [{"id": 100 + i, "value": e["value"]} for i, e in enumerate(f["enum"])]
            self.user_fields.append(f)
            return {"result": {"field": f}}
        if method == "userfieldconfig.update":
            f = next(x for x in self.user_fields if x["id"] == p["id"])
            # Настоящий портал молча игнорирует смену enum, если не указана идентичность поля.
            if not all(k in p["field"] for k in ("entityId", "fieldName", "userTypeId")):
                return {"result": {"field": f}}
            old = {e["id"]: e for e in f["enum"]}
            new = []
            for e in p["field"]["enum"]:
                if "id" in e:
                    new.append(e)
                else:
                    new.append({"id": 100 + len(old) + len(new), "value": e["value"]})
            f["enum"] = new
            return {"result": {"field": f}}
        raise AssertionError(f"unexpected method {method}")

    def _upper(self):
        return {code: {"upperName": f"UF_CRM_5_{n}"} for code, n in self._names().items()}

    @staticmethod
    def _names():
        return {
            "ufCrm5Day": "DAY", "ufCrm5PositionKey": "POSITION_KEY", "ufCrm5Number": "NUMBER", "ufCrm5Name": "NAME",
            "ufCrm5Section": "SECTION", "ufCrm5Unit": "UNIT", "ufCrm5Volume": "VOLUME", "ufCrm5Amount": "AMOUNT",
            "ufCrm5ChatId": "CHAT_ID", "ufCrm5Site": "SITE",
        }  # fmt: skip


@pytest.fixture
def portal():
    return Portal()


@pytest.fixture
def sink(portal):
    return BitrixSink(URL, 1040, FIELDS, transport=portal.transport(), sleep=lambda s: None)


def entry(num=1, value=5.0, site="11", price=2.0, day=DAY):
    return CrmEntry(
        site_name="Садик", crm_value_id=site, day=day, position_key=f"{num}|Кабель {num}",
        position_name=f"Кабель {num}", section="Электрика", unit="м", value=value, unit_price=price,
    )  # fmt: skip


def state(portal):
    return sorted((it[FIELDS["position_key"]], float(it[FIELDS["volume"]])) for it in portal.items.values())


def test_list_sites(sink):
    sites = sink.list_sites()
    assert [(s.value_id, s.name) for s in sites] == [("11", "Садик"), ("12", "Школа")]


def test_create_when_absent_and_fields(sink, portal):
    sink.upsert_entries([entry(3, 7.5, price=2.0)])
    (item,) = portal.items.values()
    assert item[FIELDS["volume"]] == 7.5
    assert item[FIELDS["amount"]] == 15.0
    assert item[FIELDS["number"]] == 3
    assert item[FIELDS["day"]] == "2026-11-03"


def test_amount_empty_without_price(sink, portal):
    sink.upsert_entries([entry(price=None)])
    (item,) = portal.items.values()
    assert item[FIELDS["amount"]] is None


def test_update_when_present_and_rerun_idempotent(sink, portal):
    sink.upsert_entries([entry(1, 5.0)])
    sink.upsert_entries([entry(1, 9.0)])
    assert state(portal) == [("1|Кабель 1", 9.0)]
    sink.upsert_entries([entry(1, 9.0)])
    assert state(portal) == [("1|Кабель 1", 9.0)]
    assert len(portal.items) == 1


def test_other_site_and_day_are_separate_items(sink, portal):
    sink.upsert_entries([entry(1, 1.0), entry(1, 2.0, site="12"), entry(1, 3.0, day=date(2026, 11, 4))])
    assert len(portal.items) == 3


def test_delete_present_and_absent(sink, portal):
    sink.upsert_entries([entry(1), entry(2)])
    sink.delete_entries([entry(1)])
    assert state(portal) == [("2|Кабель 2", 5.0)]
    sink.delete_entries([entry(1)])  # нет такого: без ошибки
    sink.delete_entries([entry(9)])
    assert len(portal.items) == 1


def test_batch_path_for_many_entries(sink, portal):
    sink.upsert_entries([entry(n, float(n)) for n in range(1, 6)])
    assert "batch" in portal.log
    assert "crm.item.add" not in portal.log
    assert len(portal.items) == 5
    sink.upsert_entries([entry(n, float(n) + 1) for n in range(1, 6)])
    assert len(portal.items) == 5
    assert state(portal)[0] == ("1|Кабель 1", 2.0)


def test_small_set_does_not_batch(sink, portal):
    sink.upsert_entries([entry(n) for n in range(1, 4)])
    assert "batch" not in portal.log


def test_more_than_50_ops_use_several_batches(sink, portal):
    sink.upsert_entries([entry(n) for n in range(1, 121)])
    assert portal.log.count("batch") == 3
    assert len(portal.items) == 120


def test_paging_finds_items_past_first_page(sink, portal):
    portal.page = 2
    sink.upsert_entries([entry(n) for n in range(1, 6)])
    sink.upsert_entries([entry(n, 8.0) for n in range(1, 6)])
    assert len(portal.items) == 5
    assert portal.log.count("crm.item.list") > 2


def test_partial_batch_failure_is_failure(sink, portal):
    sink.upsert_entries([entry(1)])
    # Портал отвечает NOT_FOUND на удаление уже удалённого элемента: одна команда падает.
    sink.upsert_entries([entry(n) for n in range(2, 6)])
    ids = list(portal.items)
    bad = [("crm.item.update", {"entityTypeId": 1040, "id": 999, "fields": {"title": "x"}})] * 3
    with pytest.raises(BitrixError, match="не выполнены"):
        sink._batch([("crm.item.update", {"entityTypeId": 1040, "id": ids[0], "fields": {"title": "ok"}}), *bad])


def test_duplicates_are_cleaned_on_upsert_and_delete(sink, portal):
    sink.upsert_entries([entry(1, 1.0)])
    portal.items[50] = dict(next(iter(portal.items.values())))
    assert len(portal.items) == 2
    sink.upsert_entries([entry(1, 4.0)])
    assert state(portal) == [("1|Кабель 1", 4.0)]
    portal.items[51] = dict(next(iter(portal.items.values())))
    sink.delete_entries([entry(1)])
    assert portal.items == {}


def test_query_limit_then_success(sink, portal):
    portal.failures = [httpx.Response(503, json={"error": "QUERY_LIMIT_EXCEEDED"})]
    assert [s.name for s in sink.list_sites()] == ["Садик", "Школа"]
    assert portal.log == ["crm.item.fields", "crm.item.fields"]


def test_http_429_and_timeout_then_success(sink, portal):
    portal.failures = [httpx.Response(429, text="slow down"), httpx.ReadTimeout("t")]
    assert len(sink.list_sites()) == 2


def test_persistent_failure_raises_bitrix_error(sink, portal):
    portal.failures = [httpx.Response(503, json={"error": "QUERY_LIMIT_EXCEEDED"})] * 10
    with pytest.raises(BitrixError, match="не отвечает"):
        sink.list_sites()
    assert portal.log.count("crm.item.fields") == 4  # первая попытка + 3 повтора


def test_transport_failure_is_crm_error(sink, portal):
    portal.failures = [httpx.ConnectError("boom")] * 10
    with pytest.raises(BitrixError):
        sink.list_sites()


def test_non_json_reply_is_error(sink, portal):
    portal.failures = [httpx.Response(200, text="<html>maintenance</html>")]
    with pytest.raises(BitrixError, match="не JSON"):
        sink.list_sites()


def test_error_body_with_http_200_is_error(sink, portal):
    portal.failures = [httpx.Response(200, json={"error": "ACCESS_DENIED", "error_description": "no scope"})]
    with pytest.raises(BitrixError, match="ACCESS_DENIED"):
        sink.list_sites()


def test_add_applied_then_5xx_retry_gives_one_item(sink, portal):
    portal.fail_after_apply = True
    portal.only = "crm.item.add"
    portal.failures = [httpx.Response(503, text="oops")]
    sink.upsert_entries([entry(1, 3.0)])  # добавление применено, ответ потерян, повтор делает то же снова
    portal.failures = []
    sink.upsert_entries([entry(1, 3.0)])
    assert len(portal.items) <= 2
    sink.upsert_entries([entry(1, 3.0)])
    assert state(portal) == [("1|Кабель 1", 3.0)]


def test_token_never_logged(sink, portal, caplog):
    caplog.set_level(logging.DEBUG)
    portal.failures = [httpx.Response(503, json={"error": "QUERY_LIMIT_EXCEEDED"}), httpx.ConnectError("x")]
    sink.upsert_entries([entry(1)])
    portal.failures = [httpx.Response(400, json={"error": "BAD", "error_description": f"{URL} broke"})]
    with pytest.raises(BitrixError) as err:
        sink.list_sites()
    assert TOKEN not in caplog.text
    assert TOKEN not in str(err.value)


def test_mask_webhook():
    assert TOKEN not in mask_webhook(f"GET {URL}crm.item.list failed")
    assert "/rest/1/***/" in mask_webhook(URL)


def test_missing_field_key_is_rejected():
    with pytest.raises(BitrixError, match="нет"):
        BitrixSink(URL, 1, {"site": "x"})


# ---- фабрика ---------------------------------------------------------------------------------
def test_factory_fake(monkeypatch):
    monkeypatch.setattr(Config, "CRM_BACKEND", "fake")
    assert isinstance(make_crm_sink(), FakeCrmSink)


def test_factory_bitrix(monkeypatch, tmp_path):
    f = tmp_path / "fields.json"
    f.write_text(json.dumps({"entityTypeId": 7, "fields": FIELDS}))
    monkeypatch.setattr(Config, "CRM_BACKEND", "bitrix")
    monkeypatch.setattr(Config, "BITRIX_WEBHOOK_URL", URL)
    monkeypatch.setattr(Config, "BITRIX_FIELDS_FILE", str(f))
    assert isinstance(make_crm_sink(), BitrixSink)


def test_factory_bitrix_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "CRM_BACKEND", "bitrix")
    monkeypatch.setattr(Config, "BITRIX_WEBHOOK_URL", "")
    with pytest.raises(BitrixError, match="BITRIX_WEBHOOK_URL"):
        make_crm_sink()
    monkeypatch.setattr(Config, "BITRIX_WEBHOOK_URL", URL)
    monkeypatch.setattr(Config, "BITRIX_FIELDS_FILE", str(tmp_path / "none.json"))
    with pytest.raises(BitrixError, match="bitrix_setup"):
        make_crm_sink()


def test_factory_unknown_backend(monkeypatch):
    monkeypatch.setattr(Config, "CRM_BACKEND", "nope")
    with pytest.raises(ValueError):
        make_crm_sink()


# ---- инструмент настройки ------------------------------------------------------------------------------
def _client(portal):
    return BitrixClient(URL, transport=portal.transport(), sleep=lambda s: None)


def test_setup_creates_everything_and_is_idempotent(portal):
    out = bitrix_setup.run_setup(_client(portal), ["Садик", "Школа"])
    assert out["entityTypeId"] == 1040
    assert out["fields"]["site"] == "ufCrm5Site"
    assert len(portal.user_fields) == len(bitrix_setup.FIELD_SPECS)
    again = bitrix_setup.run_setup(_client(portal), ["Садик", "Школа"])
    assert again == out
    assert len(portal.types) == 1
    assert len(portal.user_fields) == len(bitrix_setup.FIELD_SPECS)


def test_setup_adds_new_site_and_keeps_old_ids(portal):
    bitrix_setup.run_setup(_client(portal), ["Садик", "Школа"])
    site = next(f for f in portal.user_fields if f["fieldName"].endswith("_SITE"))
    old_ids = {e["value"]: e["id"] for e in site["enum"]}
    bitrix_setup.run_setup(_client(portal), ["Школа", "Гараж"])
    values = {e["value"]: e["id"] for e in site["enum"]}
    assert set(values) == {"Садик", "Школа", "Гараж"}  # ничего не удалено
    assert {k: values[k] for k in old_ids} == old_ids


def test_setup_keeps_every_attribute_of_old_site_values(portal):
    bitrix_setup.run_setup(_client(portal), ["Садик"])
    site = next(f for f in portal.user_fields if f["fieldName"].endswith("_SITE"))
    site["enum"][0].update(sort="300", **{"def": "Y"}, xmlId="sadik")
    bitrix_setup.run_setup(_client(portal), ["Садик", "Гараж"])
    kept = next(e for e in site["enum"] if e["value"] == "Садик")
    assert (kept["sort"], kept["def"], kept["xmlId"]) == ("300", "Y", "sadik")


def test_setup_main_writes_fields_file(portal, monkeypatch, tmp_path, capsys):
    f = tmp_path / "d" / "fields.json"
    monkeypatch.setattr(Config, "BITRIX_FIELDS_FILE", str(f))
    assert bitrix_setup.main(["--site", "Садик"], client=_client(portal)) == 0
    assert json.loads(f.read_text(encoding="utf-8"))["entityTypeId"] == 1040


def test_setup_main_needs_a_site(portal):
    with pytest.raises(SystemExit):
        bitrix_setup.main([], client=_client(portal))


def test_setup_main_reports_error_without_secret(monkeypatch, tmp_path, capsys):
    p = Portal()
    p.failures = [httpx.Response(200, json={"error": "ACCESS_DENIED", "error_description": "admin only"})]
    monkeypatch.setattr(Config, "BITRIX_FIELDS_FILE", str(tmp_path / "f.json"))
    assert bitrix_setup.main(["--site", "A"], client=_client(p)) == 1
    assert TOKEN not in capsys.readouterr().err
