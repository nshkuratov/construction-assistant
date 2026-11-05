"""Скриптовый ``Engine`` для тестов и разработки бота без сети.

Он следует плейбуку по простым текстовым правилам и вызывает настоящие обработчики MCP-инструментов
в том же процессе, поэтому сессия заканчивается в том же состоянии, что и после настоящего харнесса.
Это не языковая модель: он понимает только позиции вида ``<название работы> <число> <единица>``,
слово-день, вопрос об остатке и приветствие.
"""

import asyncio
import re

from src.config import Config
from src.mcp.cli import dispatch
from src.models import EngineOutcome
from src.session.store import SessionStore

_ITEM = re.compile(r"^(?P<name>.+?)[\s—–-]+(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[^\d\s,;]+)?$")
_REMAINING = re.compile(r"сколько\s+осталось\s+(?:по\s+)?(?P<query>.+?)\??$", re.IGNORECASE)
_DAY_WORDS = ("позавчера", "вчера")
_OPTIONS_MAX = 3


class FakeEngine:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir

    async def handle(self, chat_id: int, text: str) -> EngineOutcome:
        return await asyncio.to_thread(self._handle_sync, chat_id, text)

    def _handle_sync(self, chat_id: int, text: str) -> EngineOutcome:
        store = SessionStore(chat_id, self.data_dir)
        store.begin_turn()
        saved, Config.CA_CHAT_DIR = Config.CA_CHAT_DIR, str(store.dir.resolve())
        try:
            self._script(text.strip())
        finally:
            Config.CA_CHAT_DIR = saved
        outcome = store.load_session().turn_outcome
        return outcome or EngineOutcome(outcome="error", message="Не получилось обработать сообщение.")

    @staticmethod
    def _call(tool: str, **args) -> dict:
        envelope = dispatch(tool, args)
        if not envelope["ok"]:
            raise RuntimeError(f"{tool}: {envelope['error']['message']}")
        return envelope

    def _script(self, text: str) -> None:
        self._call("get_session")
        remaining = _REMAINING.match(text)
        if remaining:
            return self._remaining(remaining["query"])
        items = [m for part in re.split(r"[;,]|\sи\s", text) if (m := _ITEM.match(part.strip()))]
        if not items:
            self._call("send_report_form")
            self._call("finish_turn", outcome="form")
            return
        self._report(text, items)

    def _remaining(self, query: str) -> None:
        result = self._call("get_remaining", queries=[query])["results"][0]
        if result.get("position"):
            p = result["position"]
            left = p["left"]
            message = f"{p['name']}: осталось {left:g} {p['unit']}." if left is not None else f"{p['name']}: плана нет."
            self._call("finish_turn", outcome="answer", message=message)
        else:
            self._call("finish_turn", outcome="answer", message="Не нашёл такую позицию. Уточните название.")

    def _report(self, text: str, items: list[re.Match]) -> None:
        day = None
        for word in _DAY_WORDS:
            if word in text.lower():
                day = self._call("resolve_date", text=word)["day"]
                break
        results = self._call("find_positions", queries=[m["name"] for m in items])["results"]
        lines, unclear = [], []
        for m, found in zip(items, results, strict=True):
            value = float(m["value"].replace(",", "."))
            base = {"mode": "add", "value": value, "unit": m["unit"] or "", "source_text": m[0]}
            cands = found["candidates"]
            if found["found"] and not found["ambiguous"]:
                lines.append({**base, "position_id": cands[0]["position_id"]})
            elif cands:
                lines.append({**base, "candidates": [c["position_id"] for c in cands[:_OPTIONS_MAX]]})
                unclear.append(m["name"])
            else:
                unclear.append(m["name"])
        if not lines:
            self._call("ask_clarification", question="Не нашёл позицию. Уточните название работы.", source_text=text)
            self._call("finish_turn", outcome="question", message="Уточните название работы.")
            return
        self._call("propose_draft", lines=lines, day=day, replace=True)
        if unclear:
            self._call("finish_turn", outcome="question", message="Уточните: " + ", ".join(unclear))
        else:
            self._call("finish_turn", outcome="draft", message="Проверьте список и нажмите «Записать».")
