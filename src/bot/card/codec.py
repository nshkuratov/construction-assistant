"""Кодек callback data. Telegram разрешает 64 байта на кнопку, поэтому каждый аргумент это индекс или число.

Форматы: ``c1:<draft_id>:<version>:<action>[:<arg>]`` (кнопки черновика), ``q1:<turn>:<idx>``
(вариант уточнения), ``s1:<idx>`` (выбор объекта), ``r1:<kind>`` (кнопки без черновика).
"""

import re
from dataclasses import dataclass

MAX_BYTES = 64

DRAFT_ACTIONS = frozenset(
    {
        "add", "date", "sites", "saved", "ok", "cancel", "back", "del", "edit", "st", "num", "other",
        "mode", "find", "browse", "d", "dother", "nav", "pos", "cand", "pg",
    }
)  # fmt: skip
RETRY_KINDS = frozenset({"engine", "crm"})
OPEN_KINDS = frozenset({"add", "saved", "sites", "back"})

_ARG = re.compile(r"^-?\d+(:\d+)?$")
_TOKEN = re.compile(r"^[0-9a-z]{1,16}$")


class CallbackError(ValueError):
    """Callback data испорчены, чужие или слишком длинные."""


@dataclass(frozen=True)
class Action:
    name: str
    arg: str | None = None
    draft_id: str | None = None  # задаётся для кнопок c1: по нему проверяется устаревание
    version: int | None = None
    turn: str | None = None  # задаётся для кнопок q1

    def int_arg(self) -> int:
        if self.arg is None:
            raise CallbackError("the action needs a number")
        try:
            return int(self.arg)
        except ValueError as exc:
            raise CallbackError("the action argument is not a number") from exc


def _checked(data: str) -> str:
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise CallbackError("callback data is longer than 64 bytes")
    return data


def encode_card(draft_id: str, version: int, name: str, arg: int | str | None = None) -> str:
    if name not in DRAFT_ACTIONS:
        raise CallbackError(f"unknown action {name!r}")
    if not _TOKEN.match(draft_id):
        raise CallbackError("bad draft id")
    data = f"c1:{draft_id}:{version}:{name}"
    if arg is not None:
        if not _ARG.match(str(arg)):
            raise CallbackError("bad argument")
        data += f":{arg}"
    return _checked(data)


def encode_clarification(turn: str, idx: int) -> str:
    if not _TOKEN.match(turn):
        raise CallbackError("bad turn id")
    return _checked(f"q1:{turn}:{idx}")


def encode_site(idx: int) -> str:
    return _checked(f"s1:{idx}")


def encode_retry(kind: str) -> str:
    if kind not in RETRY_KINDS | OPEN_KINDS:
        raise CallbackError(f"unknown kind {kind!r}")
    return _checked(f"r1:{kind}")


def decode(data: str) -> Action:
    """Разбирает callback data в :class:`Action`; на всё остальное поднимает :class:`CallbackError`."""
    _checked(data)
    parts = data.split(":", 4)
    prefix = parts[0]
    if prefix == "c1" and len(parts) >= 4:
        _, draft_id, version, name = parts[:4]
        arg = parts[4] if len(parts) == 5 else None
        if name not in DRAFT_ACTIONS or not _TOKEN.match(draft_id) or not version.isdigit():
            raise CallbackError("bad draft callback")
        if arg is not None and not _ARG.match(arg):
            raise CallbackError("bad argument")
        return Action(name, arg, draft_id=draft_id, version=int(version))
    if prefix == "q1" and len(parts) == 3 and _TOKEN.match(parts[1]) and parts[2].isdigit():
        return Action("clar", parts[2], turn=parts[1])
    if prefix == "s1" and len(parts) == 2 and parts[1].isdigit():
        return Action("s1", parts[1])
    if prefix == "r1" and len(parts) == 2:
        if parts[1] in RETRY_KINDS:
            return Action("retry", parts[1])
        if parts[1] in OPEN_KINDS:
            return Action(parts[1])
    raise CallbackError("unknown callback")
