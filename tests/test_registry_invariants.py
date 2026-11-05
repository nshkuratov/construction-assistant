"""Инварианты реестра: набор имён инструментов зафиксирован."""

import src.mcp.cli as cli  # noqa: F401  (импорт запускает блок сборки обёрток)
from src.mcp.registry import TOOL_PHASES, TOOL_REGISTRY
from src.mcp.schemas import TOOL_ARGS

EXPECTED_TOOL_PHASES = {
    "ping": None,
    "get_session": None,
    "list_sites": None,
    "select_site": None,
    "resolve_date": None,
    "find_positions": None,
    "get_position": None,
    "get_remaining": None,
    "get_entries": None,
    "propose_draft": None,
    "ask_clarification": None,
    "send_report_form": None,
    "report_friction": None,
    "finish_turn": None,
}


def test_seed_matches_the_frozen_tool_list():
    assert TOOL_PHASES == EXPECTED_TOOL_PHASES


def test_every_registered_tool_is_seeded_and_vice_versa():
    assert set(TOOL_REGISTRY) == set(TOOL_PHASES)


def test_every_tool_has_an_argument_schema():
    assert set(TOOL_ARGS) == set(TOOL_PHASES)


def test_no_tool_is_gated():
    assert all(phase is None for phase in TOOL_PHASES.values())


def test_ping_dispatches_to_ok():
    result = cli.dispatch("ping", {})
    assert result["ok"] is True and result["status"] == "ok" and result["timestamp"]
