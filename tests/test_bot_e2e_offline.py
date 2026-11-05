"""Пример сообщения из задания через настоящие обработчики: сценарный движок и хранилища в памяти.

Подменены: сеть Telegram (``FakeBot``) и языковая модель (сценарный движок: он вызывает настоящие
инструменты MCP с одним точным запросом на каждую работу, как плейбук велит движку).
"""

import re

import pytest
from test_bot_handlers import (  # noqa: F401  (harness fixture)
    CHAT,
    DAY,
    Harness,
    harness,  # noqa: F401
    pick_site,
)

from src.engine.fake_engine import FakeEngine

MESSAGE = (
    "Сегодня проложили медь 6-ку 15 м и 9-ку 15 м, изоляцию на них столько же, кабель 3х2,5 — 56 м, "
    "поставили 10 хомутов под фреон"
)
# (запрос для find_positions, значение, строка таблицы ожидаемой позиции)
ITEMS = [
    ("фреонопровод медный Д6,35", 15, 17),
    ("фреонопровод медный Д9,52", 15, 18),
    ("трубка теплоизоляционная Д6,35", 15, 20),
    ("трубка теплоизоляционная Д9,52", 15, 21),
    ("кабель силовой 3х2,5", 56, 55),
    ("хомут для крепления фреонопровода", 10, 44),
]


class ExampleEngine(FakeEngine):
    def _script(self, text: str) -> None:
        self._call("get_session")
        results = self._call("find_positions", queries=[q for q, _, _ in ITEMS])["results"]
        lines = [
            {
                "mode": "add",
                "value": value,
                "unit": "",
                "source_text": query,
                "position_id": found["candidates"][0]["position_id"],
            }
            for (query, value, _), found in zip(ITEMS, results, strict=True)
        ]
        self._call("propose_draft", lines=lines, replace=True)
        self._call("finish_turn", outcome="draft", message="Проверьте список.")


@pytest.fixture
def example(harness):  # noqa: F811
    harness.handlers.engine = ExampleEngine(data_dir=harness.data_dir)
    return harness


async def test_example_message_ends_in_the_expected_cells_and_crm_items(example):
    h = example
    await pick_site(h)
    before = {row: h.cell("Садик", row, "T") for _, _, row in ITEMS}
    await h.text(MESSAGE)

    card = h.sent()[-1]["text"]
    assert len(re.findall(r"^\d\. ", card, flags=re.MULTILINE)) == 6
    assert h.crm.entries == {}
    assert {row: h.cell("Садик", row, "T") for _, _, row in ITEMS} == before

    await h.press(h.card_button("Записать"))

    for _, value, row in ITEMS:
        assert h.cell("Садик", row, "T") == (before[row] or 0) + value
    assert len(h.crm.entries) == 6
    assert {e.day for e in h.crm.entries.values()} == {DAY}
    assert h.sent()[-1]["text"].startswith("✅ Записано")
    assert h.store(CHAT).load_session().draft is None
