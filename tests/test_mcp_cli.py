"""Каждый путь диспетчера MCP возвращает один и тот же конверт."""

import asyncio
import json
import os
import time

import pytest

from src.config import Config
from src.mcp import cli
from src.mcp import server as mcp_server
from src.mcp.errors import NOT_IMPLEMENTED, ToolError
from src.mcp.registry import TOOL_PHASES, TOOL_REGISTRY


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def _assert_error(env, error_type):
    assert env["ok"] is False
    assert set(env) == {"ok", "error"} and set(env["error"]) == {"type", "message"}
    assert env["error"]["type"] == error_type


def test_bad_json(capsys):
    rc, env = _run(capsys, "ping", "--json", "{nope")
    assert rc == 1
    _assert_error(env, "validation")


def test_non_object_json(capsys):
    _, env = _run(capsys, "ping", "--json", "[1]")
    _assert_error(env, "validation")


def test_bad_args_name_the_field(capsys):
    _, env = _run(capsys, "select_site", "--json", "{}")
    _assert_error(env, "validation")
    assert "site" in env["error"]["message"]


def test_unknown_extra_argument_is_rejected(capsys):
    _, env = _run(capsys, "ping", "--json", '{"x": 1}')
    _assert_error(env, "validation")


def test_unknown_tool(capsys):
    rc, env = _run(capsys, "drop_tables")
    assert rc == 1
    _assert_error(env, "unknown_tool")


def test_not_implemented_error_keeps_its_type(capsys, monkeypatch):
    def stub(_params):
        raise ToolError(NOT_IMPLEMENTED, "not yet")

    monkeypatch.setitem(TOOL_REGISTRY, "get_session", stub)
    _, env = _run(capsys, "get_session")
    _assert_error(env, "not_implemented")


def test_cross_field_validation(capsys):
    _, env = _run(capsys, "get_remaining", "--json", "{}")
    _assert_error(env, "validation")


def test_success_envelope(capsys):
    rc, env = _run(capsys, "ping")
    assert rc == 0 and env["ok"] is True and env["status"] == "ok"


def test_telemetry_line_is_secret_safe(tmp_path):
    cli.dispatch("find_positions", {"queries": ["медь 6-ка секретный текст"]})
    line = json.loads(open(Config.RUN_TELEMETRY_LOG, encoding="utf-8").read().splitlines()[-1])
    assert line["tool"] == "find_positions" and line["args"] == {"n_queries": 1}
    assert "секретный" not in json.dumps(line, ensure_ascii=False)


def test_telemetry_can_be_disabled(monkeypatch):
    monkeypatch.setattr(Config, "ENABLE_RUN_TELEMETRY", False)
    cli.dispatch("ping", {})
    assert not os.path.exists(Config.RUN_TELEMETRY_LOG)


async def test_server_envelope_and_circuit_breaker(monkeypatch):
    mcp_server.reset_tool_call_count()
    monkeypatch.setattr(Config, "MCP_MAX_TOOL_CALLS_PER_RUN", 2)
    assert (await mcp_server.call_tool("ping", {}))["ok"] is True
    _assert_error(await mcp_server.call_tool("select_site", {}), "validation")
    _assert_error(await mcp_server.call_tool("ping", {}), "circuit_breaker")
    mcp_server.reset_tool_call_count()


async def test_server_serialises_calls(monkeypatch):
    mcp_server.reset_tool_call_count()
    active = {"now": 0, "max": 0}

    def slow_dispatch(tool, params):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        time.sleep(0.02)
        active["now"] -= 1
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "dispatch", slow_dispatch)
    await asyncio.gather(*(mcp_server.call_tool("ping", {}) for _ in range(5)))
    assert active["max"] == 1
    mcp_server.reset_tool_call_count()


@pytest.mark.parametrize("tool", sorted(TOOL_PHASES))
def test_every_tool_is_exposed_by_the_server(tool):
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert tool in names


async def test_fastmcp_level_rejections_use_the_envelope():
    _assert_error(await mcp_server.server.call_tool("select_site", {}), "validation")
    _assert_error(await mcp_server.server.call_tool("find_positions", {"queries": "x"}), "validation")
    _assert_error(await mcp_server.server.call_tool("drop_tables", {}), "unknown_tool")
